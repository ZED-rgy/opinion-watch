from __future__ import annotations

import asyncio
import contextlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import BrowserContext, Error, Page, Playwright, async_playwright


class BrowserProfileLocked(RuntimeError):
    """浏览器档案已经被其他 Chrome/Playwright 进程占用。"""


def _is_explicit_profile_lock_error(message: str) -> bool:
    value = message.lower()
    return "processsingleton" in value or "user data directory is already in use" in value


def _is_clean_browser_launch_exit(message: str) -> bool:
    """Chrome 把启动请求转交给已有进程时，子进程会以 0 立即退出。"""
    value = message.lower()
    return (
        "target page, context or browser has been closed" in value
        and "process did exit: exitcode=0" in value
    )


def summarize_browser_error(error: object) -> str:
    """把 Playwright/Chrome 的多行启动日志转换为可操作的用户提示。"""
    message = str(error).strip()
    if not message:
        return "浏览器启动或运行异常，账号登录状态未被改动，请重新巡检。"
    if _is_explicit_profile_lock_error(message) or _is_clean_browser_launch_exit(message):
        return (
            "Chrome 启动后立即退出，通常是账号浏览器档案仍被其他 Chrome 进程占用。"
            "账号登录状态未被改动；请关闭对应账号的 Chrome 窗口后重新巡检。"
        )
    if "target page, context or browser has been closed" in message.lower():
        return (
            "浏览器进程意外关闭，账号登录状态未被改动。"
            "请重新巡检；若持续出现，请关闭残留的 Chrome 进程后重试。"
        )
    # 普通异常只保留首行，避免 Browser logs、启动参数和调用栈淹没界面。
    first_line = next((line.strip() for line in message.splitlines() if line.strip()), message)
    if len(first_line) > 240:
        first_line = first_line[:240] + "…"
    return f"浏览器启动或运行异常，账号登录状态未被改动：{first_line}"


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
        for attempt in range(2):
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
                if _is_explicit_profile_lock_error(message):
                    raise BrowserProfileLocked(
                        f"浏览器档案正在使用中：{self.profile_dir}。请关闭对应 Chrome 后重试。"
                    ) from exc
                if _is_clean_browser_launch_exit(message):
                    if attempt == 0:
                        # Chrome 更新、上一次窗口退出或后台进程收尾时可能短暂出现
                        # “启动后正常退出”。给它一次释放档案并重启的机会。
                        await asyncio.sleep(0.75)
                        continue
                    raise BrowserProfileLocked(
                        f"浏览器档案可能仍被 Chrome 使用：{self.profile_dir}。"
                        "请关闭对应账号的 Chrome 窗口后重试。"
                    ) from exc
                raise
            return self
        raise RuntimeError("浏览器启动重试未返回结果")

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
