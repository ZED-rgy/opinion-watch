from __future__ import annotations

import contextlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import BrowserContext, Error, Page, Playwright, async_playwright


class BrowserProfileLocked(RuntimeError):
    """浏览器档案已经被其他 Chrome/Playwright 进程占用。"""


def _safe_url(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))


class BrowserSession:
    def __init__(
        self,
        profile_dir: Path,
        *,
        channel: str = "chrome",
        headless: bool = False,
        artifact_dir: Path | None = None,
    ) -> None:
        self.profile_dir = profile_dir
        self.channel = channel
        self.headless = headless
        self.artifact_dir = artifact_dir
        self.playwright: Playwright | None = None
        self.context: BrowserContext | None = None

    async def __aenter__(self) -> BrowserSession:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.playwright = await async_playwright().start()
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                channel=self.channel,
                headless=self.headless,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1440, "height": 960},
                accept_downloads=False,
            )
        except Error as exc:
            await self.playwright.stop()
            self.playwright = None
            message = str(exc)
            if "ProcessSingleton" in message or "user data directory is already in use" in message:
                raise BrowserProfileLocked(
                    f"浏览器档案正在使用中：{self.profile_dir}。请关闭对应 Chrome 后重试。"
                ) from exc
            raise
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if self.context is not None:
                await self.context.close()
        finally:
            if self.playwright is not None:
                await self.playwright.stop()

    @property
    def active_context(self) -> BrowserContext:
        if self.context is None:
            raise RuntimeError("浏览器会话尚未启动")
        return self.context

    async def page(self) -> Page:
        pages = self.active_context.pages
        return pages[0] if pages else await self.active_context.new_page()

    async def capture_diagnostic(
        self,
        page: Page,
        label: str,
        *,
        metadata: dict[str, object] | None = None,
    ) -> Path | None:
        if self.artifact_dir is None:
            return None
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "-", label).strip("-") or "diagnostic"
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = self.artifact_dir / f"{timestamp}-{safe_label}.png"
        try:
            await page.screenshot(path=str(path), full_page=False, timeout=5_000)
        except Error:
            return None
        if metadata is not None:
            details = {
                "captured_at": datetime.now(UTC).isoformat(),
                "url": _safe_url(page.url),
                "label": label,
                **metadata,
            }
            with contextlib.suppress(OSError):
                path.with_suffix(".json").write_text(
                    json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        return path
