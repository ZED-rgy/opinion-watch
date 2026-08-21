from __future__ import annotations

import re
from urllib.parse import quote

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

    def parse_content_id(self, url: str) -> str | None:
        match = self._content_pattern.search(url)
        return match.group(1).lower() if match else None

    def accepts_url(self, url: str) -> bool:
        return "xiaohongshu.com" in url and self._content_pattern.search(url) is not None
