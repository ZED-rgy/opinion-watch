from __future__ import annotations

import re
from contextlib import suppress
from urllib.parse import quote

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from opinion_watch.collectors.base import BaseCollector
from opinion_watch.models import AnchorCandidate, Platform


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
        'a[href*="/user/"]',
    )
    comment_selectors = (
        '[data-e2e="comment-item"]',
        '[class*="comment-item"]',
        '[class*="commentItem"]',
    )

    def build_search_url(self, keyword: str) -> str:
        return f"https://www.douyin.com/jingxuan/search/{quote(keyword)}"

    async def _extract_anchors(self, page: Page) -> list[AnchorCandidate]:
        raw_cards = await self._evaluate_elements(
            page,
            '[id^="waterfall_item_"]',
            """
            cards => cards.map(card => ({
                id: card.id || '',
                text: (card.innerText || card.textContent || '').trim()
            }))
            """,
        )
        cards: list[AnchorCandidate] = []
        for card in raw_cards:
            element_id = str(card.get("id", ""))
            content_id = element_id.removeprefix("waterfall_item_")
            if not content_id.isdigit():
                continue
            text = str(card.get("text", ""))
            content_kind = "note" if text.lstrip().startswith("图文") else "video"
            cards.append(
                AnchorCandidate(
                    href=f"https://www.douyin.com/{content_kind}/{content_id}",
                    text=text,
                    media_kind="image" if content_kind == "note" else "video",
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

    def parse_content_id(self, url: str) -> str | None:
        match = self._content_pattern.search(url)
        return match.group(1) if match else None

    def accepts_url(self, url: str) -> bool:
        return "douyin.com" in url and self._content_pattern.search(url) is not None
