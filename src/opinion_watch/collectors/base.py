from __future__ import annotations

import asyncio
import hashlib
import json
import random
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

from opinion_watch.models import (
    AnchorCandidate,
    CollectedContent,
    DetailStatus,
    Platform,
    SessionStatus,
)


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
    login_confirmation_phrases: tuple[str, ...] = ()
    login_wall_selectors: tuple[str, ...] = ()
    title_selectors: tuple[str, ...] = ()
    description_selectors: tuple[str, ...] = ()
    author_selectors: tuple[str, ...] = ()
    comment_selectors: tuple[str, ...] = ()
    media_container_selectors: tuple[str, ...] = ()
    detail_view_selectors: tuple[str, ...] = ()
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
    unavailable_phrases = (
        "当前笔记无法浏览",
        "你访问的页面不见了",
        "页面不见了",
        "笔记已删除",
        "笔记不存在",
        "内容不存在",
        "暂时无法浏览",
    )

    def __init__(self) -> None:
        self._search_diagnostics: dict[str, str] = {}

    @staticmethod
    def _human_delay_ms(base_ms: int, *, jitter: float = 0.45) -> int:
        """给固定等待加随机抖动；机械的固定节奏是风控的显著特征。"""
        low = int(base_ms * (1 - jitter))
        high = int(base_ms * (1 + jitter))
        return random.randint(max(200, low), max(400, high))

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

        if any(phrase in body_text for phrase in self.login_confirmation_phrases):
            return SessionStatus.VERIFICATION_REQUIRED
        for selector in self.login_wall_selectors:
            with suppress(Exception):
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    return SessionStatus.VERIFICATION_REQUIRED
        if any(phrase in body_text for phrase in self.verification_phrases):
            return SessionStatus.VERIFICATION_REQUIRED
        if any(phrase in body_text for phrase in self.rate_limit_phrases):
            return SessionStatus.RATE_LIMITED
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
        artifact_dir: Path | None = None,
        diagnostic_key: str | None = None,
    ) -> list[CollectedContent]:
        search_url = self.build_search_url(keyword)
        await self._open_search_page(page, search_url, keyword)

        await page.wait_for_timeout(self._human_delay_ms(1_500))
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
                    break

            if len(results) >= limit:
                break

            unchanged_rounds = unchanged_rounds + 1 if len(results) == before else 0
            if unchanged_rounds >= self._search_unchanged_rounds:
                break
            await page.mouse.wheel(0, random.randint(900, 1_600))
            # Some platform pages render the next batch asynchronously after
            # the scroll event. Give the DOM a little more time before taking
            # the next snapshot, otherwise a quick scan can stop at 9-10 cards
            # even though more public results are available.
            await page.wait_for_timeout(self._human_delay_ms(1_800))
            anchors = await self._extract_anchors(page)

        quality = self.search_quality(results)
        if quality["needs_diagnostic"] and artifact_dir is not None:
            diagnostic_path = await self._save_search_diagnostic(
                page,
                artifact_dir=artifact_dir,
                keyword=keyword,
                quality=quality,
            )
            if diagnostic_path:
                key = diagnostic_key or keyword
                self._search_diagnostics[key] = diagnostic_path
                results = [
                    replace(
                        item,
                        raw_data={
                            **item.raw_data,
                            "search_diagnostic_path": diagnostic_path,
                            "search_quality": quality,
                        },
                    )
                    for item in results
                ]
        return results

    @staticmethod
    def search_quality(items: list[CollectedContent]) -> dict[str, Any]:
        total = len(items)
        empty_title = sum(not str(item.title or "").strip() for item in items)
        usable_text = sum(
            bool(
                str(item.title or "").strip()
                or str(item.raw_data.get("search_card_text") or "").strip()
            )
            for item in items
        )
        empty_title_ratio = empty_title / total if total else 0.0
        return {
            "total": total,
            "empty_title": empty_title,
            "usable_text": usable_text,
            "empty_title_ratio": round(empty_title_ratio, 3),
            "needs_diagnostic": total == 0 or empty_title_ratio >= 0.3,
            "all_titles_empty": total > 0 and empty_title == total,
        }

    def pop_search_diagnostic(self, key: str) -> str | None:
        return self._search_diagnostics.pop(key, None)

    async def _save_search_diagnostic(
        self,
        page: Page,
        *,
        artifact_dir: Path,
        keyword: str,
        quality: dict[str, Any],
    ) -> str | None:
        """保存搜索页截图和可复盘的 URL/选择器/质量信息。"""
        safe_keyword = re.sub(r"[^a-zA-Z0-9_-]+", "-", keyword).strip("-")[:80] or "keyword"
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        base = artifact_dir / f"{timestamp}-{safe_keyword}-search-quality"
        screenshot_path = base.with_suffix(".png")
        metadata_path = base.with_suffix(".json")
        metadata = {
            "captured_at": datetime.now(UTC).isoformat(),
            "platform": self.platform.value,
            "keyword": keyword,
            # Search links can contain short-lived xsec_token parameters.  A
            # diagnostic must remain useful without becoming a credential log.
            "url": self.canonical_url(page.url),
            "selector": self.content_link_selector,
            "quality": quality,
        }
        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(screenshot_path), full_page=False, timeout=5_000)
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return str(screenshot_path)
        except Exception:
            with suppress(Exception):
                metadata_path.write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            return None

    async def _open_search_page(self, page: Page, search_url: str, keyword: str) -> None:
        """先加载平台首页建立前端环境，再打开搜索页。"""
        if not page.url.startswith(self.home_url):
            with suppress(PlaywrightTimeoutError):
                await page.goto(self.home_url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(self._human_delay_ms(1_000))
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
        search_page: Page | None = None,
    ) -> list[CollectedContent]:
        """只打开疑似内容的详情页，补充正文、评论和媒体证据。"""
        if detail_limit <= 0 or not items:
            return items

        enriched = [
            replace(
                item,
                raw_data={
                    **item.raw_data,
                    "detail_status": DetailStatus.NOT_SELECTED.value,
                },
            )
            for item in items
        ]
        detail_page: Page | None = None
        restore_search_url: str | None = None
        popup_page: Page | None = None
        try:
            candidate_indexes = [
                index
                for index, item in enumerate(items)
                if detail_candidate_ids is None or item.content_id in detail_candidate_ids
            ][:detail_limit]
            for index in candidate_indexes:
                if restore_search_url and search_page is not None:
                    await self._restore_search_page(search_page, restore_search_url)
                    restore_search_url = None
                if popup_page is not None:
                    with suppress(Exception):
                        await popup_page.close()
                    popup_page = None
                item = items[index]
                attempting = {
                    **item.raw_data,
                    "detail_status": DetailStatus.ATTEMPTING.value,
                    "detail_checked_at": datetime.now(UTC).isoformat(),
                    "detail_error": "",
                }
                enriched[index] = replace(item, raw_data=attempting)
                click_error = ""
                try:
                    if search_page is not None:
                        (
                            detail_page,
                            restore_search_url,
                            click_error,
                        ) = await self._open_detail_by_click(search_page, item)
                        if detail_page is None:
                            enriched[index] = replace(
                                item,
                                raw_data={
                                    **attempting,
                                    "detail_status": DetailStatus.FAILED.value,
                                    "detail_checked_at": attempting["detail_checked_at"],
                                    "detail_error": click_error or "搜索卡片点击后未进入详情页",
                                },
                            )
                            continue
                        if detail_page is not search_page:
                            popup_page = detail_page
                    else:
                        detail_page = await context.new_page()
                        popup_page = detail_page
                        await detail_page.goto(
                            item.navigation_url or item.url,
                            wait_until="domcontentloaded",
                            timeout=60_000,
                        )
                except PlaywrightTimeoutError:
                    # 超时后详情页可能仍停留在上一条内容上；此时抽取会把别人的
                    # 正文、评论和媒体写进当前条目。跳过详情补充，保留浅层结果。
                    if detail_page is None or self.canonical_url(
                        detail_page.url
                    ) != self.canonical_url(item.url):
                        enriched[index] = replace(
                            item,
                            raw_data={
                                **attempting,
                                "detail_status": DetailStatus.FAILED.value,
                                "detail_checked_at": attempting["detail_checked_at"],
                                "detail_error": "详情页导航超时或未进入目标内容页",
                            },
                        )
                        continue
                    # A timeout can still leave a usable detail document.
                except PlaywrightError as exc:
                    enriched[index] = replace(
                        item,
                        raw_data={
                            **attempting,
                            "detail_status": DetailStatus.FAILED.value,
                            "detail_checked_at": attempting["detail_checked_at"],
                            "detail_error": str(exc)[:1000],
                        },
                    )
                    continue
                if detail_page is None:
                    continue
                await detail_page.wait_for_timeout(self._human_delay_ms(1_500))
                status = await self.session_status(detail_page, context)
                if (
                    status is SessionStatus.VERIFICATION_REQUIRED
                    and await self._dismiss_login_modal(detail_page)
                ):
                    await detail_page.wait_for_timeout(800)
                    status = await self.session_status(detail_page, context)
                if status is SessionStatus.VERIFICATION_REQUIRED:
                    # cookie 有效但详情页仍被登录引导墙盖住：跳过这一条的
                    # 详情补充，保留浅层结果，不让整个关键词的采集作废。
                    enriched[index] = replace(
                        item,
                        raw_data={
                            **attempting,
                            "detail_status": DetailStatus.FAILED.value,
                            "detail_checked_at": attempting["detail_checked_at"],
                            "detail_error": "详情页被登录确认或验证拦截",
                        },
                    )
                    continue
                if status is not SessionStatus.HEALTHY:
                    enriched[index] = replace(
                        item,
                        raw_data={
                            **attempting,
                            "detail_status": DetailStatus.FAILED.value,
                            "detail_checked_at": attempting["detail_checked_at"],
                            "detail_error": f"详情页会话状态：{status.value}",
                        },
                    )
                    continue

                page_title = await detail_page.title()
                title = await self._first_text(detail_page, self.title_selectors)
                description = await self._first_text(detail_page, self.description_selectors)
                author_name = await self._first_text(detail_page, self.author_selectors)
                body_text = ""
                with suppress(Exception):
                    body_text = (await detail_page.locator("body").inner_text(timeout=2_000))[
                        :20_000
                    ]
                unavailable_reason = self._unavailable_reason(page_title, body_text)
                valid_title = self._valid_detail_text(title, page_title)
                valid_author = self._valid_detail_author(author_name)
                if unavailable_reason:
                    enriched[index] = replace(
                        item,
                        raw_data={
                            **attempting,
                            "card_title": item.raw_data.get("card_title", item.title),
                            "card_author_name": item.raw_data.get(
                                "card_author_name", item.author_name
                            ),
                            "detail_title": title[:500],
                            "display_title": item.title[:500],
                            "detail_status": DetailStatus.UNAVAILABLE.value,
                            "detail_checked_at": attempting["detail_checked_at"],
                            "detail_error": unavailable_reason,
                            "unavailable_reason": unavailable_reason,
                            "detail_collected": False,
                            "page_title": page_title[:500],
                        },
                    )
                    continue
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
                    "card_title": item.raw_data.get("card_title", item.title),
                    "card_author_name": item.raw_data.get("card_author_name", item.author_name),
                    "detail_title": title[:500],
                    "display_title": (title if valid_title else item.title or page_title)[:500],
                    "detail_status": DetailStatus.SUCCEEDED.value,
                    "detail_checked_at": attempting["detail_checked_at"],
                    "detail_error": "",
                    "description": description,
                    "comments": comments,
                    "media": media,
                }
                enriched[index] = replace(
                    item,
                    title=(title if valid_title else item.title or page_title)[:500],
                    author_name=(author_name if valid_author else item.author_name)[:200],
                    raw_data=raw_data,
                )
        finally:
            if restore_search_url and search_page is not None:
                await self._restore_search_page(search_page, restore_search_url)
            if popup_page is not None:
                with suppress(Exception):
                    await popup_page.close()
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
            media_locator = None
            for selector in self.media_container_selectors:
                candidate = page.locator(selector).first
                if await candidate.count() and await candidate.is_visible():
                    media_locator = candidate
                    break
            if media_locator is None:
                media_locator = page.locator("main img, main video")
            handles = []
            with suppress(PlaywrightError):
                tag_name = str(await media_locator.evaluate("node => node.tagName.toLowerCase()"))
                if tag_name in {"img", "video"}:
                    own_handle = await media_locator.element_handle()
                    if own_handle is not None:
                        handles.append(own_handle)
            handles.extend(await media_locator.locator("img, video").element_handles())
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
                            height: node.naturalHeight || node.videoHeight || 0,
                            id: node.id || '',
                            className: typeof node.className === 'string' ? node.className : '',
                            parentClass: node.parentElement
                              && typeof node.parentElement.className === 'string'
                              ? node.parentElement.className
                              : '',
                            ancestorClass: node.parentElement && node.parentElement.parentElement
                              && typeof node.parentElement.parentElement.className === 'string'
                              ? node.parentElement.parentElement.className
                              : ''
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
            source_url = str(node.get("src") or "")
            if source_url.startswith(("data:", "blob:")):
                source_url = str(node.get("poster") or "")
            if not self._is_relevant_media(node, source_url):
                continue
            entry: dict[str, Any] = {
                "kind": "image" if kind == "img" else "video_keyframe",
                "url": source_url[:2000],
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

    async def _open_detail_by_click(
        self,
        search_page: Page,
        item: CollectedContent,
    ) -> tuple[Page | None, str | None, str]:
        """Open a detail view through the live search card, never its tokenized href."""
        # 小红书卡片通常同时渲染一个隐藏的规范链接和一个真正可见的
        # 带临时访问参数的卡片链接。必须点击可见节点，不能取 DOM 中的
        # 第一个链接，否则 Playwright 会一直等待隐藏锚点变为可点击。
        locator = search_page.locator(f'a[href*="{item.content_id}"]:visible').first
        try:
            if not await locator.count():
                return None, None, "搜索页找不到对应卡片"
            before_url = search_page.url
            popup: Page | None = None
            try:
                async with search_page.expect_popup(timeout=4_000) as popup_info:
                    await locator.click(timeout=5_000, no_wait_after=True)
                popup = await popup_info.value
            except PlaywrightTimeoutError:
                # 没有新窗口并不等于点击失败：小红书常在当前页改路由或打开弹层。
                popup = None
            if popup is not None:
                with suppress(Exception):
                    await popup.wait_for_load_state("domcontentloaded", timeout=10_000)
                await popup.wait_for_timeout(500)
                if self.accepts_url(popup.url) or await self._has_detail_view(popup):
                    return popup, None, ""
                with suppress(Exception):
                    await popup.close()
                return None, None, "卡片新窗口未进入可识别的详情页"

            await search_page.wait_for_timeout(800)
            if search_page.url != before_url and self.accepts_url(search_page.url):
                return search_page, before_url, ""
            if await self._has_detail_view(search_page):
                return search_page, before_url, ""
            return None, None, "卡片点击后未进入详情页"
        except PlaywrightError as exc:
            return None, None, str(exc)[:1000]

    async def _has_detail_view(self, page: Page) -> bool:
        selectors = self.detail_view_selectors or self.title_selectors
        for selector in selectors:
            with suppress(PlaywrightError):
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    return True
        return False

    async def _restore_search_page(self, search_page: Page, search_url: str) -> None:
        """Return a current-page detail view to the original search context."""
        if search_page.url != search_url:
            with suppress(Exception):
                await search_page.go_back(wait_until="domcontentloaded", timeout=10_000)
        if search_page.url != search_url:
            with suppress(Exception):
                await search_page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)

    @staticmethod
    def _is_relevant_media(node: dict[str, Any], source_url: str) -> bool:
        """过滤头像、图标、徽章、占位图和过小的静态资源。"""
        if not source_url or source_url.startswith(("data:", "blob:")):
            return False
        lowered = " ".join(
            str(node.get(key) or "")
            for key in ("id", "className", "parentClass", "ancestorClass", "alt")
        ).lower()
        static_tokens = (
            "avatar",
            "logo",
            "icon",
            "favicon",
            "sprite",
            "badge",
            "emoji",
            "qrcode",
            "qr-code",
            "header",
            "nav",
            "头像",
            "网站图标",
            "小图标",
            "默认头像",
        )
        if any(token in lowered for token in static_tokens):
            return False
        width = int(node.get("width") or 0)
        height = int(node.get("height") or 0)
        if width < 120 or height < 120 or width * height < 22_500:
            return False
        ratio = width / height if height else 0
        return 0.1 <= ratio <= 10

    def items_from_anchors(
        self, anchors: list[AnchorCandidate], keyword: str
    ) -> list[CollectedContent]:
        items: list[CollectedContent] = []
        for anchor in anchors:
            if not self.accepts_url(anchor.href):
                continue
            content_id = self.parse_content_id(anchor.href) or self._fallback_id(anchor.href)
            title = " ".join(anchor.text.split())[:500]
            canonical = self.canonical_url(anchor.href)
            items.append(
                CollectedContent(
                    platform=self.platform,
                    content_id=content_id,
                    url=canonical,
                    title=title,
                    source_keyword=keyword,
                    raw_data={
                        "source": "browser_dom",
                        "media_kind": anchor.media_kind,
                        "search_card_text": anchor.raw_text or anchor.text,
                        "card_title": title,
                        "card_author_name": anchor.author_name,
                        "detail_status": DetailStatus.NOT_SELECTED.value,
                    },
                    author_name=anchor.author_name,
                    navigation_url=anchor.href,
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

    @classmethod
    def _unavailable_reason(cls, *values: str) -> str:
        text = " ".join(value for value in values if value).strip()
        for phrase in cls.unavailable_phrases:
            if phrase in text:
                return phrase
        return ""

    @classmethod
    def _valid_detail_text(cls, value: str, page_title: str = "") -> bool:
        clean = " ".join(value.split())
        if not clean or cls._unavailable_reason(clean, page_title):
            return False
        return clean not in {"抖音", "小红书", "无标题", "页面"}

    @staticmethod
    def _valid_detail_author(value: str) -> bool:
        clean = " ".join(value.split()).lstrip("@")
        return bool(clean) and clean not in {"我的", "关注", "登录", "收藏", "分享", "用户"}

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
