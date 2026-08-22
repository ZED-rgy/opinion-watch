from __future__ import annotations

import re
from contextlib import suppress
from urllib.parse import quote

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from opinion_watch.collectors.base import BaseCollector
from opinion_watch.models import AnchorCandidate, Platform


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
    # 主选择器失效时回退到通用锚点扫描：accepts_url 仍会按内容 ID 模式
    # 过滤，宁可多抓再过滤，也不要因为一次改版而静默归零。
    content_link_selector = (
        'a.title[href*="/search_result/"], section a[href*="/search_result/"], a[href*="/explore/"]'
    )
    title_selectors = (
        "#detail-title",
        '[class*="title"]',
        'meta[property="og:title"]',
    )
    detail_view_selectors = (
        "#detail-title",
        "#noteContainer",
        '[class*="note-detail" i]',
        '[class*="note-content" i]',
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
    media_container_selectors = (
        "#noteContainer",
        '[class*="note-content" i]',
        '[class*="note-detail" i]',
        '[class*="media-container" i]',
        '[class*="swiper" i]',
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
        # 首页也可能直接盖登录引导弹窗，挡住搜索框；先关掉。
        with suppress(Exception):
            await self._dismiss_login_modal(page)
            await page.wait_for_timeout(500)
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
                await page.wait_for_url("**/search_result**", timeout=8_000)
            except PlaywrightTimeoutError:
                # 部分版本的搜索框不响应回车，需要点搜索按钮。
                search_button = page.locator(
                    ".search-icon, .input-button, #search-input ~ * [class*='search' i]"
                ).first
                with suppress(PlaywrightError):
                    await search_button.click(timeout=3_000)
                    await page.wait_for_url("**/search_result**", timeout=8_000)
                    return
            else:
                return
        await super()._open_search_page(page, search_url, keyword)

    def parse_content_id(self, url: str) -> str | None:
        match = self._content_pattern.search(url)
        return match.group(1).lower() if match else None

    def accepts_url(self, url: str) -> bool:
        return "xiaohongshu.com" in url and self._content_pattern.search(url) is not None

    async def _extract_anchors(self, page: Page) -> list[AnchorCandidate]:
        """从笔记卡片容器抽取标题和作者，而不是只读封面链接文本。

        小红书的搜索卡片经常把 `/explore/...` 链接只放在封面图片上，标题、作者
        位于同级节点。这里从链接向上寻找卡片容器，再在容器内提取结构化字段，
        同时保留整张卡片文本供品牌归属和规则筛选使用。
        """
        raw_items = await self._evaluate_elements(
            page,
            self.content_link_selector,
            """
            anchors => {
              const clean = value => (value || '').replace(/\\s+/g, ' ').trim();
              const noise = new RegExp(
                '^(图文|视频|笔记|直播|关注|分享|收藏|评论|'
                + '\\d+(?:\\.\\d+)?[万亿]?|\\d{2}-\\d{2}|\\d{4}-\\d{2}-\\d{2})$'
              );
              const textOf = node => clean(node && (node.innerText || node.textContent || ''));
              const firstText = (root, selectors) => {
                for (const selector of selectors) {
                  const node = root.querySelector(selector);
                  const value = textOf(node) || clean(node && (
                    node.getAttribute('title') || node.getAttribute('aria-label')
                  ));
                  if (value && !noise.test(value)) return value;
                }
                return '';
              };
              const findCard = anchor => {
                let node = anchor;
                let fallback = anchor.parentElement || anchor;
                for (let depth = 0; node && depth < 7; depth += 1, node = node.parentElement) {
                  const value = textOf(node);
                  if (value.length >= 12 && value.length <= 2500) fallback = node;
                  const isSmallSection = node.matches('section')
                    && node.querySelectorAll(
                      'a[href*="/explore/"], a[href*="/search_result/"]'
                    ).length <= 2;
                  if (isSmallSection || node.matches(
                    'article, [class*="note" i], [class*="card" i], [id*="note" i]'
                  )) return node;
                }
                return fallback;
              };
              const fallbackTitle = card => {
                const values = textOf(card).split(/\\n+/).map(clean).filter(Boolean);
                const candidates = values.filter(
                  value => value.length >= 3 && !noise.test(value) && !value.startsWith('@')
                );
                return candidates.sort((left, right) => right.length - left.length)[0] || '';
              };
              return anchors.map(anchor => {
                const card = findCard(anchor);
                const cardText = textOf(card);
                const title = firstText(card, [
                  '[class*="title" i]', '[data-testid*="title" i]',
                  '[aria-label*="标题" i]', 'h1', 'h2', 'h3'
                ]) || clean(anchor.getAttribute('title')) || clean(
                  anchor.querySelector('img')?.getAttribute('alt')
                ) || fallbackTitle(card);
                const author = firstText(card, [
                  '[class*="author" i] [class*="name" i]', '[class*="user" i] [class*="name" i]',
                  '[class*="author" i]', '[class*="user" i]', 'a[href*="/user/profile/"]'
                ]);
                const mediaKind = card.querySelector('video')
                  || /video/i.test(card.className || '') ? 'video' : 'image';
                return {
                  href: anchor.href || '',
                  text: clean(title),
                  author_name: clean(author).replace(/^@/, ''),
                  raw_text: cardText,
                  media_kind: mediaKind
                };
              });
            }
            """,
        )
        return [
            AnchorCandidate(
                href=str(item.get("href", "")),
                text=str(item.get("text", "")),
                media_kind=str(item.get("media_kind", "")),
                author_name=str(item.get("author_name", "")),
                raw_text=str(item.get("raw_text", "")),
            )
            for item in raw_items
            if item.get("href")
        ]
