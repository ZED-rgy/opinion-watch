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
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

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
    require_detail_content_id = False
    prefer_direct_detail_navigation = False
    detail_identity_stability_checks = 1
    detail_identity_poll_ms = 250
    suspend_detail_media_playback = False
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
    def _search_access_message(status: SessionStatus) -> str:
        messages = {
            SessionStatus.LOGIN_REQUIRED: "搜索页确认账号登录已失效，请重新登录。",
            SessionStatus.VERIFICATION_REQUIRED: (
                "搜索页出现登录确认或验证遮罩；已尝试关闭遮罩和备用入口，账号登录状态未改变。"
            ),
            SessionStatus.CAPTCHA_REQUIRED: (
                "平台要求完成人机验证（验证码/滑块）。本轮已停止，请在浏览器中人工完成验证后重试；"
                "系统不会尝试绕过验证。"
            ),
            SessionStatus.RATE_LIMITED: ("平台搜索访问暂时受限，请稍后重试；账号登录状态未改变。"),
            SessionStatus.ERROR: "平台搜索页暂时异常，请按重试策略再次执行。",
        }
        return messages.get(status, f"搜索页状态异常：{status.value}")

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
        # 人机验证优先于登录墙选择器判定：验证遮罩常常复用登录弹窗的类名，
        # 一旦被归入 VERIFICATION_REQUIRED 就会触发关闭弹窗/切换备用路由的
        # 恢复动作，那等同于绕过验证。
        if any(phrase in body_text for phrase in self.verification_phrases):
            return SessionStatus.CAPTCHA_REQUIRED
        for selector in self.login_wall_selectors:
            with suppress(Exception):
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
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
        upstream_error = await self._upstream_error(page)
        if upstream_error:
            raise CollectorRuntimeError(SessionStatus.ERROR, upstream_error)
        status = await self.session_status(page, context)
        # 命中人机验证时立即终止：不关遮罩、不换路由，交由人工处理。
        if status is SessionStatus.CAPTCHA_REQUIRED:
            raise CollectorRuntimeError(status, self._search_access_message(status))
        # 登录态健康但页面盖着登录引导弹窗时（session_status 已经用 cookie
        # 区分了这种情况），先尝试关闭弹窗再继续，而不是把整个平台的本轮
        # 巡检直接判死。
        if status is SessionStatus.VERIFICATION_REQUIRED and await self._dismiss_login_modal(page):
            await page.wait_for_timeout(800)
            status = await self.session_status(page, context)
            if status is SessionStatus.CAPTCHA_REQUIRED:
                raise CollectorRuntimeError(status, self._search_access_message(status))
        if status is not SessionStatus.HEALTHY and await self._recover_search_access(
            page, context, keyword, status
        ):
            await page.wait_for_timeout(self._human_delay_ms(1_000))
            upstream_error = await self._upstream_error(page)
            if upstream_error:
                raise CollectorRuntimeError(SessionStatus.ERROR, upstream_error)
            status = await self.session_status(page, context)
        if status is not SessionStatus.HEALTHY:
            raise CollectorRuntimeError(status, self._search_access_message(status))

        results: list[CollectedContent] = []
        seen_ids: set[str] = set()
        unchanged_rounds = 0
        anchors = await self._wait_for_content_anchors(page, timeout_ms=20_000)
        if not any(self.accepts_url(anchor.href) for anchor in anchors):
            status = await self.session_status(page, context)
            if status is not SessionStatus.HEALTHY:
                raise CollectorRuntimeError(
                    status,
                    self._search_access_message(status),
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

    async def _upstream_error(self, page: Page) -> str | None:
        """识别网关错误页，避免把平台故障当成零搜索结果。"""
        title = ""
        body = ""
        with suppress(Exception):
            title = await page.title()
        with suppress(Exception):
            body = (await page.locator("body").inner_text(timeout=2_000))[:8_000]
        text = f"{title}\n{body}"
        if not re.search(r"\b(?:502|503|504)\b", text):
            return None
        if not re.search(
            r"bad gateway|service unavailable|gateway timeout|upstream|tengine|网关|上游服务",
            text,
            re.IGNORECASE,
        ):
            return None
        status_code = re.search(r"\b(?:502|503|504)\b", text)
        code = status_code.group(0) if status_code else "5xx"
        return f"平台搜索页返回 HTTP {code} 网关错误（可能是上游服务异常），将按重试策略处理。"

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
        no_usable_text = total > 0 and usable_text == 0
        return {
            "total": total,
            "empty_title": empty_title,
            "usable_text": usable_text,
            "empty_title_ratio": round(empty_title_ratio, 3),
            "needs_diagnostic": total == 0 or empty_title_ratio >= 0.3,
            "degrades_run": no_usable_text or empty_title_ratio >= 0.3,
            "all_titles_empty": total > 0 and empty_title == total,
            # 标题解析退化和"整页无法提取任何文本"是两回事。前者仍然留有
            # 卡片原文可供品牌匹配，只该降级告警；只有后者才说明这一轮
            # 检索完全没有可用证据，值得判定为数据质量失败。
            "no_usable_text": no_usable_text,
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
            if not self._reached_search_page(page.url, search_url):
                raise CollectorRuntimeError(
                    SessionStatus.ERROR,
                    f"{self.platform.value} 搜索页导航超时，未能进入目标搜索结果页。",
                ) from exc

    @staticmethod
    def _reached_search_page(actual_url: str, search_url: str) -> bool:
        """判断超时后浏览器是否已经停在目标搜索页。

        不能用字符串全等：平台一定会改写搜索 URL（小红书追加 &type=51、
        排序和来源参数等），全等比较会把已经渲染好的正常页面误判成导航超时。
        判定规则是 scheme/host/path 一致，且请求里带的每个查询参数都在实际
        URL 中原值出现——后者保证关键词没有被平台换掉，平台自己追加的额外
        参数不影响结果。
        """
        actual = urlsplit(actual_url)
        expected = urlsplit(search_url)
        if (actual.scheme.lower(), actual.netloc.lower()) != (
            expected.scheme.lower(),
            expected.netloc.lower(),
        ):
            return False
        # 路径里的关键词可能一侧是百分号编码、另一侧是原文，先统一解码。
        if unquote(actual.path).rstrip("/") != unquote(expected.path).rstrip("/"):
            return False
        actual_query = parse_qs(actual.query)
        return all(
            set(values).issubset(set(actual_query.get(key, [])))
            for key, values in parse_qs(expected.query).items()
        )

    async def _recover_search_access(
        self,
        page: Page,
        context: BrowserContext,
        keyword: str,
        status: SessionStatus,
    ) -> bool:
        """Allow a platform adapter to switch away from a route-level login wall."""
        return False

    async def _dismiss_login_modal(self, page: Page) -> bool:
        """尝试关闭登录引导弹窗；确认遮罩消失才返回 True。"""
        for selector in self.login_modal_close_selectors:
            with suppress(Exception):
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    await locator.click(timeout=2_000)
                    return True
        # 通用回退：多数登录弹窗都响应 Esc。按下后必须复核遮罩确实消失，
        # 否则"恒真"的返回值会让调用方误以为已经恢复，白白多跑一轮判定。
        if not self.login_wall_selectors:
            return False
        with suppress(Exception):
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
            for selector in self.login_wall_selectors:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    return False
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
                    if search_page is not None and not self.prefer_direct_detail_navigation:
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
                        if self.suspend_detail_media_playback:
                            await self._install_media_playback_guard(detail_page)
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
                identity_matches, identity_error = await self._confirm_stable_detail_identity(
                    detail_page,
                    item,
                )
                if not identity_matches:
                    if detail_page is not search_page:
                        with suppress(Exception):
                            await detail_page.close()
                        popup_page = None
                    detail_page = None
                    enriched[index] = replace(
                        item,
                        raw_data={
                            **attempting,
                            "detail_status": DetailStatus.FAILED.value,
                            "detail_checked_at": attempting["detail_checked_at"],
                            "detail_error": identity_error,
                        },
                    )
                    continue
                await detail_page.wait_for_timeout(self._human_delay_ms(1_500))
                if self.suspend_detail_media_playback:
                    await self._suspend_media_playback(detail_page)
                identity_matches, identity_error = await self._detail_identity_matches(
                    detail_page,
                    item,
                )
                if not identity_matches:
                    if detail_page is not search_page:
                        with suppress(Exception):
                            await detail_page.close()
                        popup_page = None
                    detail_page = None
                    enriched[index] = replace(
                        item,
                        raw_data={
                            **attempting,
                            "detail_status": DetailStatus.FAILED.value,
                            "detail_checked_at": attempting["detail_checked_at"],
                            "detail_error": identity_error,
                        },
                    )
                    continue
                status = await self.session_status(detail_page, context)
                if status is SessionStatus.CAPTCHA_REQUIRED:
                    # 详情页命中人机验证：不做任何关闭/绕过尝试，直接中止
                    # 本平台剩余详情采集，交由人工处理。
                    raise CollectorRuntimeError(status, self._search_access_message(status))
                if (
                    status is SessionStatus.VERIFICATION_REQUIRED
                    and await self._dismiss_login_modal(detail_page)
                ):
                    await detail_page.wait_for_timeout(800)
                    status = await self.session_status(detail_page, context)
                    if status is SessionStatus.CAPTCHA_REQUIRED:
                        raise CollectorRuntimeError(status, self._search_access_message(status))
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
                # 抖音等 SPA 可能在详情抽取期间自动切换到下一条推荐内容。
                # 写入前必须再次确认页面仍属于当前候选，禁止混入其他视频证据。
                identity_matches, identity_error = await self._detail_identity_matches(
                    detail_page,
                    item,
                )
                if not identity_matches:
                    if detail_page is not search_page:
                        with suppress(Exception):
                            await detail_page.close()
                        popup_page = None
                    detail_page = None
                    enriched[index] = replace(
                        item,
                        raw_data={
                            **attempting,
                            "detail_status": DetailStatus.FAILED.value,
                            "detail_checked_at": attempting["detail_checked_at"],
                            "detail_error": identity_error,
                        },
                    )
                    continue
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
            handles = []
            if media_locator is None:
                # 回退分支必须直接取这些 img/video 元素本身。原先套用容器逻辑
                # 去 img/video 内部再找 img/video，结果恒为空，且多元素定位器
                # 触发的 strict 违规异常被 suppress 吞掉，选择器改版后会静默
                # 退化成"这条内容没有媒体证据"。
                handles.extend(await page.locator("main img, main video").element_handles())
            else:
                with suppress(PlaywrightError):
                    tag_name = str(
                        await media_locator.evaluate("node => node.tagName.toLowerCase()")
                    )
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
        # before_url 必须在 try 之前取好，点击可能在抛异常前就已经改了路由。
        before_url = search_page.url
        try:
            if not await locator.count():
                # 没点到任何东西，搜索页原样未动，不需要复位。
                return None, None, "搜索页找不到对应卡片"
            popup: Page | None = None
            try:
                async with search_page.expect_popup(timeout=4_000) as popup_info:
                    await locator.click(timeout=5_000, no_wait_after=True)
                popup = await popup_info.value
            except PlaywrightTimeoutError:
                # 没有新窗口并不等于点击失败：小红书常在当前页改路由或打开弹层。
                popup = None
            if popup is not None:
                # 详情开在新窗口里，搜索页保持原样，关掉弹窗即可。
                with suppress(Exception):
                    await popup.wait_for_load_state("domcontentloaded", timeout=10_000)
                await popup.wait_for_timeout(500)
                matches, identity_error = await self._detail_identity_matches(popup, item)
                if matches:
                    return popup, None, ""
                with suppress(Exception):
                    await popup.close()
                return None, None, identity_error

            await search_page.wait_for_timeout(800)
            matches, identity_error = await self._detail_identity_matches(search_page, item)
            if matches:
                return search_page, before_url, ""
            # 关键：点击已经把搜索页路由到了别的详情页，失败时也必须把
            # before_url 交回调用方去复位。否则搜索页永久停在错误详情页上，
            # 该关键词剩余候选会全部以"搜索页找不到对应卡片"失败，
            # 错因还被记成卡片问题而不是这次错配。
            return None, before_url, identity_error
        except PlaywrightError as exc:
            return None, before_url, str(exc)[:1000]

    async def _detail_identity_matches(
        self,
        page: Page,
        item: CollectedContent,
    ) -> tuple[bool, str]:
        """确认详情页属于当前候选，防止推荐弹窗污染证据。"""
        actual_content_id = self.parse_content_id(page.url)
        if actual_content_id:
            if actual_content_id == item.content_id:
                return True, ""
            return (
                False,
                f"详情内容错配：期望 {item.content_id}，实际 {actual_content_id}",
            )
        if self.require_detail_content_id:
            return False, f"详情页无法确认内容 ID：期望 {item.content_id}"
        if self.accepts_url(page.url) or await self._has_detail_view(page):
            return True, ""
        return False, "卡片点击后未进入可识别的详情页"

    async def _confirm_stable_detail_identity(
        self,
        page: Page,
        item: CollectedContent,
    ) -> tuple[bool, str]:
        """在 SPA 路由稳定窗口内持续确认详情 ID，捕获延迟推荐跳转。"""
        checks = max(1, self.detail_identity_stability_checks)
        for check_index in range(checks):
            matches, identity_error = await self._detail_identity_matches(page, item)
            if not matches:
                return False, identity_error
            if check_index < checks - 1:
                await page.wait_for_timeout(max(50, self.detail_identity_poll_ms))
        return True, ""

    async def _install_media_playback_guard(self, page: Page) -> None:
        """在详情文档加载前阻止自动播放，避免播完后跳到下一条推荐。"""
        script = """
        (() => {
          if (window.__opinionWatchPlaybackGuard) return;
          window.__opinionWatchPlaybackGuard = true;
          const pause = node => {
            if (!(node instanceof HTMLMediaElement)) return;
            node.autoplay = false;
            node.loop = true;
            try { node.pause(); } catch (_) {}
          };
          document.addEventListener('play', event => pause(event.target), true);
          document.addEventListener('loadedmetadata', event => pause(event.target), true);
          new MutationObserver(records => {
            for (const record of records) {
              for (const node of record.addedNodes) {
                if (!(node instanceof Element)) continue;
                pause(node);
                node.querySelectorAll('video, audio').forEach(pause);
              }
            }
          }).observe(document.documentElement, {childList: true, subtree: true});
          document.querySelectorAll('video, audio').forEach(pause);
        })();
        """
        with suppress(Exception):
            await page.add_init_script(script)

    async def _suspend_media_playback(self, page: Page) -> None:
        """兜底暂停已经挂载的媒体元素。"""
        with suppress(Exception):
            await page.locator("video, audio").evaluate_all(
                "elements => elements.forEach(element => {"
                "element.autoplay = false; element.loop = true; element.pause();})"
            )

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
