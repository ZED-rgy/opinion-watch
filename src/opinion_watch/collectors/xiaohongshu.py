from __future__ import annotations

import re
from contextlib import suppress
from urllib.parse import quote

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from opinion_watch.collectors.base import BaseCollector
from opinion_watch.models import Platform


class XiaohongshuCollector(BaseCollector):
    platform = Platform.XIAOHONGSHU
    home_url = "https://www.xiaohongshu.com/"
    authenticated_cookie_names = frozenset({"web_session"})
    authenticated_ui_selectors = (
        '[data-e2e*="avatar" i]',
        'header [class*="avatar" i]',
        'header img[alt*="头像"]',
    )
    login_required_phrases = ("登录后查看搜索结果", "登录后查看")
    login_modal_close_selectors = (
        '.login-container [class*="close" i]',
        '[class*="login-modal" i] [class*="close" i]',
        '[class*="loginContainer" i] [class*="close" i]',
    )
    _content_pattern = re.compile(r"/(?:explore|discovery/item|search_result)/([0-9a-fA-F]{16,32})")
    content_link_selector = 'a.title[href*="/search_result/"]'
    title_selectors = (
        "#detail-title",
        '[class*="title"]',
        'meta[property="og:title"]',
    )
    description_selectors = (
        "#detail-desc",
        '[class*="desc"]',
        'meta[property="og:description"]',
    )
    author_selectors = (
        '.author-wrapper [class*="name"]',
        '.author-container [class*="name"]',
        'a[href*="/user/profile/"]',
    )
    comment_selectors = (
        '.comment-item [class*="content"]',
        '[class*="comment-item"] [class*="content"]',
        '[class*="commentItem"] [class*="content"]',
    )

    def build_search_url(self, keyword: str) -> str:
        encoded = quote(keyword)
        return (
            "https://www.xiaohongshu.com/search_result"
            f"?keyword={encoded}&source=web_search_result_notes"
        )

    async def _open_search_page(self, page: Page, search_url: str, keyword: str) -> None:
        """优先在首页搜索框中输入关键词搜索。

        直接深链跳转 search_result 页面即使在已登录会话里也经常触发
        “登录后查看搜索结果”登录墙；模拟真实的搜索路径可以明显降低
        触发概率。搜索框不可用时回退到深链方式。
        """
        await super()._open_search_page(page, self.home_url, keyword)
        search_box = page.locator("#search-input, input[placeholder*='搜索']").first
        with suppress(PlaywrightError):
            await search_box.wait_for(state="visible", timeout=5_000)
            await search_box.click()
            await search_box.press("Control+A")
            await search_box.press("Backspace")
            await page.wait_for_timeout(300)
            await search_box.press_sequentially(keyword, delay=80)
            if await search_box.input_value() != keyword:
                await search_box.fill(keyword)
                await page.wait_for_timeout(300)
            await search_box.press("Enter")
            try:
                await page.wait_for_url("**/search_result**", timeout=15_000)
            except PlaywrightTimeoutError:
                pass
            else:
                return
        await super()._open_search_page(page, search_url, keyword)

    def parse_content_id(self, url: str) -> str | None:
        match = self._content_pattern.search(url)
        return match.group(1).lower() if match else None

    def accepts_url(self, url: str) -> bool:
        return "xiaohongshu.com" in url and self._content_pattern.search(url) is not None
