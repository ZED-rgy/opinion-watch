from __future__ import annotations

import asyncio
import hashlib
import re
from abc import ABC, abstractmethod
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from opinion_watch.models import AnchorCandidate, CollectedContent, Platform, SessionStatus


class CollectorRuntimeError(RuntimeError):
    def __init__(self, status: SessionStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


class BaseCollector(ABC):
    platform: Platform
    home_url: str
    authenticated_cookie_names: frozenset[str]
    authenticated_ui_selectors: tuple[str, ...] = ()
    login_required_phrases: tuple[str, ...] = ()
    title_selectors: tuple[str, ...] = ()
    description_selectors: tuple[str, ...] = ()
    author_selectors: tuple[str, ...] = ()
    comment_selectors: tuple[str, ...] = ()
    # 平台可能在登录态健康时仍然弹出登录引导弹窗；这些选择器用于尝试
    # 关闭它而不是直接放弃本轮检索。
    login_modal_close_selectors: tuple[str, ...] = ()
    _dom_evaluation_timeout_seconds = 6
    _search_scroll_rounds = 12
    _search_unchanged_rounds = 3
    content_link_selector = "a[href]"
    verification_phrases = (
        "请完成验证",
        "请完成下列验证后继续",
        "完成上方拼图",
        "安全验证",
        "拖动滑块",
        "验证码",
    )
    rate_limit_phrases = ("访问频繁", "操作频繁", "请求过于频繁", "请稍后再试")

    @abstractmethod
    def build_search_url(self, keyword: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def parse_content_id(self, url: str) -> str | None:
        raise NotImplementedError

    @abstractmethod
    def accepts_url(self, url: str) -> bool:
        raise NotImplementedError

    async def session_status(self, page: Page, context: BrowserContext) -> SessionStatus:
        url = page.url.lower()
        if any(token in url for token in ("login", "passport", "signin")):
            return SessionStatus.LOGIN_REQUIRED

        body_text = ""
        with suppress(Exception):
            body_text = (await page.locator("body").inner_text(timeout=3_000))[:20_000]

        if any(phrase in body_text for phrase in self.rate_limit_phrases):
            return SessionStatus.RATE_LIMITED
        if any(phrase in body_text for phrase in self.verification_phrases):
            return SessionStatus.VERIFICATION_REQUIRED
        if any(phrase in body_text for phrase in self.login_required_phrases):
            # A platform can show a login modal on a search route while the
            # account cookie is still present. Report this as a route-level
            # verification issue instead of incorrectly marking the account
            # itself as logged out.
            with suppress(Exception):
                cookies = await context.cookies()
                names = {cookie["name"] for cookie in cookies}
                if names.intersection(self.authenticated_cookie_names):
                    return SessionStatus.VERIFICATION_REQUIRED
            return SessionStatus.LOGIN_REQUIRED

        # Do not restrict the cookie query to one URL: platforms may scope the
        # current session cookie to a parent domain or a non-root path.
        with suppress(Exception):
            cookies = await context.cookies()
            names = {cookie["name"] for cookie in cookies}
            if names.intersection(self.authenticated_cookie_names):
                return SessionStatus.HEALTHY
        for selector in self.authenticated_ui_selectors:
            with suppress(Exception):
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    return SessionStatus.HEALTHY
        return SessionStatus.LOGIN_REQUIRED

    async def search(
        self,
        page: Page,
        context: BrowserContext,
        keyword: str,
        *,
        limit: int = 20,
    ) -> list[CollectedContent]:
        search_url = self.build_search_url(keyword)
        await self._open_search_page(page, search_url, keyword)

        await page.wait_for_timeout(1_500)
        status = await self.session_status(page, context)
        # 登录态健康但页面盖着登录引导弹窗时（session_status 已经用 cookie
        # 区分了这种情况），先尝试关闭弹窗再继续，而不是把整个平台的本轮
        # 巡检直接判死。
        if status is SessionStatus.VERIFICATION_REQUIRED and await self._dismiss_login_modal(page):
            await page.wait_for_timeout(800)
            status = await self.session_status(page, context)
        if status is not SessionStatus.HEALTHY:
            raise CollectorRuntimeError(status, f"{self.platform.value} 会话状态：{status.value}")

        results: list[CollectedContent] = []
        seen_ids: set[str] = set()
        unchanged_rounds = 0
        anchors = await self._wait_for_content_anchors(page, timeout_ms=20_000)
        if not any(self.accepts_url(anchor.href) for anchor in anchors):
            status = await self.session_status(page, context)
            if status is not SessionStatus.HEALTHY:
                raise CollectorRuntimeError(
                    status,
                    f"{self.platform.value} 搜索结果页状态：{status.value}",
                )

        for _ in range(self._search_scroll_rounds):
            before = len(results)
            for item in self.items_from_anchors(anchors, keyword):
                if item.content_id in seen_ids:
                    continue
                seen_ids.add(item.content_id)
                results.append(item)
                if len(results) >= limit:
                    return results

            unchanged_rounds = unchanged_rounds + 1 if len(results) == before else 0
            if unchanged_rounds >= self._search_unchanged_rounds:
                break
            await page.mouse.wheel(0, 1_200)
            # Some platform pages render the next batch asynchronously after
            # the scroll event. Give the DOM a little more time before taking
            # the next snapshot, otherwise a quick scan can stop at 9-10 cards
            # even though more public results are available.
            await page.wait_for_timeout(1_800)
            anchors = await self._extract_anchors(page)

        return results

    async def _open_search_page(self, page: Page, search_url: str, keyword: str) -> None:
        """先加载平台首页建立前端环境，再打开搜索页。"""
        if not page.url.startswith(self.home_url):
            with suppress(PlaywrightTimeoutError):
                await page.goto(self.home_url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(1_000)
        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
        except PlaywrightTimeoutError as exc:
            # 页面超时时也可能已经完成跳转并渲染出可用内容。但如果地址栏仍停留在
            # 旧页面，继续抽取会把上一个关键词的结果记到当前关键词名下。
            if page.url != search_url:
                raise CollectorRuntimeError(
                    SessionStatus.ERROR,
                    f"{self.platform.value} 搜索页导航超时，未能进入目标搜索结果页。",
                ) from exc

    async def _dismiss_login_modal(self, page: Page) -> bool:
        """尝试关闭登录引导弹窗；成功关闭任意一个即返回 True。"""
        for selector in self.login_modal_close_selectors:
            with suppress(Exception):
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    await locator.click(timeout=2_000)
                    return True
        # 通用回退：多数登录弹窗都响应 Esc。
        with suppress(Exception):
            await page.keyboard.press("Escape")
            return True
        return False

    async def enrich_items(
        self,
        context: BrowserContext,
        items: list[CollectedContent],
        *,
        detail_limit: int = 5,
        comments_limit: int = 20,
        detail_candidate_ids: set[str] | None = None,
        artifact_dir: Path | None = None,
    ) -> list[CollectedContent]:
        """只打开疑似内容的详情页，补充正文、评论和媒体证据。"""
        if detail_limit <= 0 or not items:
            return items

        enriched = list(items)
        detail_page = await context.new_page()
        try:
            candidate_indexes = [
                index
                for index, item in enumerate(items)
                if detail_candidate_ids is None or item.content_id in detail_candidate_ids
            ][:detail_limit]
            for index in candidate_indexes:
                item = items[index]
                try:
                    await detail_page.goto(
                        item.url,
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                except PlaywrightTimeoutError:
                    # 超时后详情页可能仍停留在上一条内容上；此时抽取会把别人的
                    # 正文、评论和媒体写进当前条目。跳过详情补充，保留浅层结果。
                    if self.canonical_url(detail_page.url) != self.canonical_url(item.url):
                        continue
                await detail_page.wait_for_timeout(1_500)
                status = await self.session_status(detail_page, context)
                if status is not SessionStatus.HEALTHY:
                    raise CollectorRuntimeError(
                        status,
                        f"{self.platform.value} 详情页会话状态：{status.value}",
                    )

                page_title = await detail_page.title()
                title = await self._first_text(detail_page, self.title_selectors)
                description = await self._first_text(detail_page, self.description_selectors)
                author_name = await self._first_text(detail_page, self.author_selectors)
                comments = await self._all_text(
                    detail_page,
                    self.comment_selectors,
                    limit=comments_limit,
                )
                media = await self._extract_media_evidence(
                    detail_page,
                    artifact_dir=artifact_dir,
                    content_id=item.content_id,
                )
                raw_data = {
                    **item.raw_data,
                    "detail_collected": True,
                    "page_title": page_title,
                    "description": description,
                    "comments": comments,
                    "media": media,
                }
                enriched[index] = replace(
                    item,
                    title=(title or item.title or page_title)[:500],
                    author_name=(author_name or item.author_name)[:200],
                    raw_data=raw_data,
                )
        finally:
            await detail_page.close()
        return enriched

    async def _extract_media_evidence(
        self,
        page: Page,
        *,
        artifact_dir: Path | None,
        content_id: str,
    ) -> list[dict[str, Any]]:
        """Capture public image/video DOM evidence without downloading arbitrary files."""
        # 元数据和截图必须来自同一个元素句柄。懒加载页面的 DOM 顺序会变，
        # 先快照再按下标重新定位会把截图挂到别的媒体条目上。
        try:
            handles = await page.locator("img, video").element_handles()
        except PlaywrightError:
            return []
        evidence: list[dict[str, Any]] = []
        for index, handle in enumerate(handles[:8]):
            try:
                node = await asyncio.wait_for(
                    handle.evaluate(
                        """
                        node => ({
                            kind: node.tagName.toLowerCase(),
                            src: node.currentSrc || node.src || '',
                            poster: node.poster || '',
                            alt: node.alt || node.getAttribute('aria-label') || '',
                            width: node.naturalWidth || node.videoWidth || 0,
                            height: node.naturalHeight || node.videoHeight || 0
                        })
                        """
                    ),
                    timeout=self._dom_evaluation_timeout_seconds,
                )
            except (TimeoutError, PlaywrightError):
                continue
            if not isinstance(node, dict):
                continue
            kind = str(node.get("kind") or "")
            if kind not in {"img", "video"}:
                continue
            entry: dict[str, Any] = {
                "kind": "image" if kind == "img" else "video_keyframe",
                "url": str(node.get("src") or "")[:2000],
                "poster": str(node.get("poster") or "")[:2000],
                "alt": str(node.get("alt") or "")[:500],
                "width": int(node.get("width") or 0),
                "height": int(node.get("height") or 0),
            }
            if artifact_dir is not None:
                safe_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", content_id)[:80] or "content"
                timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                path = artifact_dir / f"{timestamp}-{safe_id}-{index}.png"
                with suppress(Exception):
                    artifact_dir.mkdir(parents=True, exist_ok=True)
                    await handle.screenshot(path=str(path), timeout=5_000)
                    entry["evidence_path"] = str(path)
            evidence.append(entry)
        return evidence

    def items_from_anchors(
        self, anchors: list[AnchorCandidate], keyword: str
    ) -> list[CollectedContent]:
        items: list[CollectedContent] = []
        for anchor in anchors:
            if not self.accepts_url(anchor.href):
                continue
            content_id = self.parse_content_id(anchor.href) or self._fallback_id(anchor.href)
            title = " ".join(anchor.text.split())[:500]
            items.append(
                CollectedContent(
                    platform=self.platform,
                    content_id=content_id,
                    url=anchor.href,
                    title=title,
                    source_keyword=keyword,
                    raw_data={
                        "source": "browser_dom",
                        "media_kind": anchor.media_kind,
                    },
                )
            )
        return items

    async def _extract_anchors(self, page: Page) -> list[AnchorCandidate]:
        raw_items = await self._evaluate_elements(
            page,
            self.content_link_selector,
            """
            anchors => anchors.map(anchor => ({
                href: anchor.href || '',
                text: (
                    anchor.innerText
                    || anchor.textContent
                    || anchor.getAttribute('aria-label')
                    || ''
                ).trim()
            }))
            """,
        )
        return [
            AnchorCandidate(href=str(item.get("href", "")), text=str(item.get("text", "")))
            for item in raw_items
            if item.get("href")
        ]

    async def _evaluate_elements(
        self,
        page: Page,
        selector: str,
        expression: str,
    ) -> list[dict[str, Any]]:
        raw_items: list[dict[str, Any]] = []
        for attempt in range(4):
            # 指数退避：持续跳转的页面在固定短间隔内往往仍不稳定。
            backoff_ms = 750 * (2**attempt)
            try:
                raw_items = await asyncio.wait_for(
                    page.locator(selector).evaluate_all(expression),
                    timeout=self._dom_evaluation_timeout_seconds,
                )
                break
            except TimeoutError as exc:
                if attempt == 3:
                    raise CollectorRuntimeError(
                        SessionStatus.ERROR,
                        "页面 DOM 抽取多次超时，可能正在持续跳转或加载。",
                    ) from exc
                await page.wait_for_timeout(backoff_ms)
            except PlaywrightError as exc:
                message = str(exc).lower()
                navigation_changed_context = (
                    "execution context was destroyed" in message
                    or "most likely because of a navigation" in message
                )
                if not navigation_changed_context:
                    raise
                if attempt == 3:
                    raise CollectorRuntimeError(
                        SessionStatus.ERROR,
                        "页面持续跳转，无法获得稳定的搜索结果 DOM。",
                    ) from exc
                await page.wait_for_timeout(backoff_ms)
        return raw_items

    async def _wait_for_content_anchors(
        self,
        page: Page,
        *,
        timeout_ms: int,
    ) -> list[AnchorCandidate]:
        """等待客户端渲染搜索结果，同时保留最后一次公开链接快照。"""
        interval_ms = 1_000
        attempts = max(1, timeout_ms // interval_ms)
        anchors: list[AnchorCandidate] = []
        for attempt in range(attempts):
            anchors = await self._extract_anchors(page)
            if any(self.accepts_url(anchor.href) for anchor in anchors):
                return anchors
            if attempt < attempts - 1:
                await page.wait_for_timeout(interval_ms)
        return anchors

    @staticmethod
    def canonical_url(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))

    def _fallback_id(self, url: str) -> str:
        canonical = self.canonical_url(url).encode()
        return "url-" + hashlib.sha256(canonical).hexdigest()[:24]

    @staticmethod
    async def _first_text(page: Page, selectors: tuple[str, ...]) -> str:
        for selector in selectors:
            locator = page.locator(selector).first
            with suppress(Exception):
                text = " ".join((await locator.inner_text(timeout=1_500)).split())
                if not text:
                    text = " ".join(((await locator.get_attribute("content")) or "").split())
                if text:
                    return text
        return ""

    @staticmethod
    async def _all_text(page: Page, selectors: tuple[str, ...], *, limit: int) -> list[str]:
        if limit <= 0:
            return []
        results: list[str] = []
        seen: set[str] = set()
        for selector in selectors:
            locator = page.locator(selector)
            with suppress(Exception):
                count = min(await locator.count(), limit)
                for index in range(count):
                    text = " ".join((await locator.nth(index).inner_text(timeout=1_000)).split())
                    if text and text not in seen:
                        seen.add(text)
                        results.append(text[:2_000])
                        if len(results) >= limit:
                            return results
            if results:
                break
        return results
