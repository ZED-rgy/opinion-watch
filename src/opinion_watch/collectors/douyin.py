from __future__ import annotations

import re
from contextlib import suppress
from urllib.parse import parse_qs, quote, urlsplit

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from opinion_watch.collectors.base import BaseCollector
from opinion_watch.models import AnchorCandidate, Platform, SessionStatus


class DouyinCollector(BaseCollector):
    platform = Platform.DOUYIN
    home_url = "https://www.douyin.com/"
    authenticated_cookie_names = frozenset({"sessionid", "sessionid_ss", "sid_guard"})
    authenticated_ui_selectors = (
        '[data-e2e="header-avatar"]',
        '[data-e2e*="avatar" i]',
        'header [class*="avatar" i]',
        'header img[alt*="头像"]',
    )
    login_confirmation_phrases = (
        "登录后即可搜索更多精彩视频",
        "登录后查看更多精彩视频",
        "登录确认中",
        "请在抖音APP确认登录",
        "请在抖音 app 确认登录",
    )
    login_wall_selectors = (
        '[class*="login-modal" i]',
        '[class*="login-panel" i]',
        '[class*="device-confirm" i]',
        '[class*="verify" i][role="dialog"]',
    )
    _content_pattern = re.compile(r"/(?:video|note)/(\d+)")
    content_link_selector = 'a[href*="/video/"], a[href*="/note/"]'
    title_selectors = (
        '[data-e2e="video-desc"]',
        'meta[property="og:title"]',
        "h1",
    )
    description_selectors = (
        '[data-e2e="video-desc"]',
        'meta[property="og:description"]',
    )
    author_selectors = (
        '[data-e2e="video-author-name"]',
        '[data-e2e*="author" i]',
        '[class*="author-name" i]',
        'main a[href*="/user/"]',
    )
    comment_selectors = (
        '[data-e2e="comment-item"]',
        '[class*="comment-item"]',
        '[class*="commentItem"]',
    )
    media_container_selectors = (
        '[data-e2e="video-player"]',
        '[data-e2e*="player" i]',
        '[class*="video-player" i]',
        '[class*="player-container" i]',
    )
    require_detail_content_id = True
    prefer_direct_detail_navigation = True
    # 抖音详情是 SPA 路由，初始目标 URL 可能在数秒后被推荐流替换。
    # 持续约 4 秒确认内容 ID，并暂停视频，避免播放结束自动切换。
    detail_identity_stability_checks = 17
    detail_identity_poll_ms = 250
    suspend_detail_media_playback = True

    def build_search_url(self, keyword: str) -> str:
        return f"https://www.douyin.com/jingxuan/search/{quote(keyword)}"

    _CARD_NOISE = re.compile(
        r"^(图文|视频|直播)$|^@|^\d+(\.\d+)?[万亿]?$|^\d{2}-\d{2}$|^\d{4}-\d{2}-\d{2}$"
    )

    @classmethod
    def _clean_card_text(cls, text: str) -> str:
        """从搜索卡片整段文本中提取正文标题。

        卡片 innerText 形如 "图文\n标题内容#话题\n@作者\n1.2万\n08-21"，
        直接入库会把媒体标记、点赞数和日期都带进标题。
        """
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        candidates = [line for line in lines if not cls._CARD_NOISE.match(line)]
        # 取最长的一行作为标题主体；卡片里正文几乎总是最长的文本行。
        return max(candidates, key=len) if candidates else " ".join(lines)

    @classmethod
    def _card_author_name(cls, text: str, dom_author: str = "") -> str:
        """提取卡片作者名。

        DOM 里能直接读到作者节点时优先用它；否则回退到 innerText 中的
        "@作者" 行。作者名是品牌归属判断的重要证据（品牌常以官号名出现），
        不能像标题清洗那样被当作噪声丢掉。
        """
        cleaned = " ".join(dom_author.split()).lstrip("@").strip()
        if cleaned:
            return cleaned
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("@") and len(line) > 1:
                return line[1:].strip()
        return ""

    async def _extract_anchors(self, page: Page) -> list[AnchorCandidate]:
        raw_cards = await self._evaluate_elements(
            page,
            '[id^="waterfall_item_"]',
            """
            cards => cards.map(card => {
                const link = card.matches('a[href*="/video/"], a[href*="/note/"]')
                    ? card
                    : card.querySelector('a[href*="/video/"], a[href*="/note/"]');
                const text = (card.innerText || card.textContent || '').trim();
                const authorNode = card.querySelector(
                    '[data-e2e*="author" i], [class*="author" i], a[href*="/user/"]'
                );
                return {
                    id: card.id || '',
                    href: link && link.href ? link.href : '',
                    text,
                    author: authorNode
                        ? (authorNode.innerText || authorNode.textContent || '').trim()
                        : '',
                    hasMedia: Boolean(card.querySelector('img, video, picture, canvas')),
                    isRelatedSearch: text.includes('相关搜索') || Boolean(
                        card.querySelector('[class*="related-search" i], [data-e2e*="related" i]')
                    )
                };
            })
            """,
        )
        cards: list[AnchorCandidate] = []
        for card in raw_cards:
            if bool(card.get("isRelatedSearch")):
                continue
            href = str(card.get("href") or "")
            linked_content_id = self.parse_content_id(href) if href else None
            element_id = str(card.get("id", ""))
            fallback_content_id = element_id.removeprefix("waterfall_item_")
            if linked_content_id:
                content_id = linked_content_id
            elif bool(card.get("hasMedia")) and fallback_content_id.isdigit():
                # 少数真实卡片没有可读 href，但一定带有媒体封面。不能只凭
                # waterfall_item_<数字> 推断，否则“相关搜索”会被误造为视频。
                content_id = fallback_content_id
            else:
                continue
            text = str(card.get("text", ""))
            content_kind = (
                "note"
                if "/note/" in urlsplit(href).path or text.lstrip().startswith("图文")
                else "video"
            )
            cards.append(
                AnchorCandidate(
                    href=href or f"https://www.douyin.com/{content_kind}/{content_id}",
                    text=self._clean_card_text(text),
                    media_kind="image" if content_kind == "note" else "video",
                    author_name=self._card_author_name(text, str(card.get("author") or "")),
                    # 标题清洗只留最长一行；品牌名可能出现在话题标签、作者行
                    # 或次要文本里。保留整段卡片文本作为品牌归属的判断依据。
                    raw_text=text,
                )
            )
        return cards or await super()._extract_anchors(page)

    async def _open_search_page(self, page: Page, search_url: str, keyword: str) -> None:
        await super()._open_search_page(page, self.home_url, keyword)
        search_box = page.locator('[data-e2e="searchbar-input"]').first
        search_button = page.locator('[data-e2e="searchbar-button"]').first
        with suppress(PlaywrightError):
            await search_box.wait_for(state="visible", timeout=5_000)
            await search_box.click()
            await search_box.press("Control+A")
            await search_box.press("Backspace")
            await page.wait_for_timeout(300)
            await search_box.press_sequentially(keyword, delay=100)
            if await search_box.input_value() != keyword:
                await search_box.fill(keyword)
                await page.wait_for_timeout(300)
            await search_button.click(timeout=5_000)
            try:
                await page.wait_for_url("**/jingxuan/search/**", timeout=15_000)
            except PlaywrightTimeoutError:
                pass
            else:
                return

        await super()._open_search_page(page, search_url, keyword)

    async def _recover_search_access(
        self,
        page: Page,
        context: BrowserContext,
        keyword: str,
        status: SessionStatus,
    ) -> bool:
        """精选搜索出现一键登录墙时，切换到普通搜索路由。"""
        # 人机验证一律不做路由切换：换入口继续抓取等同于绕过验证。
        if status is not SessionStatus.VERIFICATION_REQUIRED:
            return False
        fallback_url = f"https://www.douyin.com/search/{quote(keyword)}"
        if self.canonical_url(page.url) == self.canonical_url(fallback_url):
            return False
        try:
            await page.goto(fallback_url, wait_until="domcontentloaded", timeout=30_000)
        except PlaywrightTimeoutError:
            if self.canonical_url(page.url) != self.canonical_url(fallback_url):
                return False
        return True

    def parse_content_id(self, url: str) -> str | None:
        parts = urlsplit(url)
        hostname = (parts.hostname or "").lower()
        if hostname != "douyin.com" and not hostname.endswith(".douyin.com"):
            return None
        match = self._content_pattern.search(parts.path)
        if match:
            return match.group(1)
        modal_ids = parse_qs(parts.query).get("modal_id", [])
        modal_id = str(modal_ids[0]) if modal_ids else ""
        return modal_id if modal_id.isdigit() else None

    def accepts_url(self, url: str) -> bool:
        return self.parse_content_id(url) is not None
