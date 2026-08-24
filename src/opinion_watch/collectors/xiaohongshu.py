from __future__ import annotations

import re
from contextlib import suppress
from typing import Any
from urllib.parse import quote

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from opinion_watch.collectors.base import BaseCollector
from opinion_watch.models import AnchorCandidate, CollectedContent, Platform


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

    @staticmethod
    def search_quality(items: list[CollectedContent]) -> dict[str, Any]:
        """Tolerate normal image-only notes without hiding selector regressions.

        小红书允许正文标题为空、只在封面图中放文字。30% 空标题仍保存诊断
        截图，但不应把整轮巡检判成 partial；接近整页丢失标题时才降级。
        """
        quality = BaseCollector.search_quality(items)
        quality["degrades_run"] = bool(quality["no_usable_text"]) or (
            float(quality["empty_title_ratio"]) >= 0.8
        )
        return quality

    def build_search_url(self, keyword: str) -> str:
        encoded = quote(keyword)
        return (
            "https://www.xiaohongshu.com/search_result"
            f"?keyword={encoded}&source=web_search_result_notes"
        )

    # 卡片页脚的时间戳形态很多："08-03"、"2025-12-03"、"昨天 13:10"、
    # "3天前"、"19小时前"、"刚刚"。作者节点经常把它们一起包进来。
    _AUTHOR_TRAILING_TIME = re.compile(
        r"(?:\s|^)(?:"
        r"\d{4}-\d{2}-\d{2}|\d{2}-\d{2}"
        r"|(?:昨天|今天|前天)(?:\s*\d{1,2}:\d{2})?"
        r"|\d+\s*(?:分钟|小时|天|周|个月|月|年)前"
        r"|刚刚"
        r")(?:\s*\d{1,2}:\d{2})?\s*$"
    )
    # 单独成行时不构成标题的卡片元素：媒体标记、互动数、时间戳。
    _CARD_NOISE_TOKEN = re.compile(
        r"^(?:图文|视频|笔记|直播|关注|分享|收藏|评论|赞|点赞"
        r"|\d+(?:\.\d+)?[万亿]?"
        r"|\d{4}-\d{2}-\d{2}|\d{2}-\d{2}"
        r"|(?:昨天|今天|前天)(?:\s*\d{1,2}:\d{2})?"
        r"|\d+\s*(?:分钟|小时|天|周|个月|月|年)前"
        r"|刚刚"
        r"|\d{1,2}:\d{2})$"
    )

    @classmethod
    def _normalize_author_name(cls, value: str) -> str:
        """剥掉作者名尾部的发布时间。

        小红书卡片的作者节点常常连着时间戳一起渲染，直接入库会得到
        "boss炸雷 04-24" 这种作者名，既无法聚合同一作者，也会让标题
        回退逻辑把整行误当成标题。
        """
        cleaned = " ".join(value.split()).lstrip("@").strip()
        # 时间戳可能叠加（"昨天 13:10"），重复剥离直到稳定。
        while True:
            stripped = cls._AUTHOR_TRAILING_TIME.sub("", cleaned).strip()
            if stripped == cleaned:
                return cleaned
            cleaned = stripped

    @classmethod
    def _is_author_echo_title(cls, title: str, author: str) -> bool:
        """判断标题是否只是"作者名 + 时间 + 互动数"的回声。

        卡片没有标题节点时，回退逻辑会取最长的一行，而图文笔记的最长行
        往往就是作者行。这类标题不含任何正文信息，却会让空标题质量闸门
        误判为"解析正常"，从而掩盖选择器失效。
        """
        title = " ".join(title.split())
        author = " ".join(author.split())
        if not title or not author or not title.startswith(author):
            return False
        remainder = title[len(author) :].strip()
        if not remainder:
            return True
        return all(cls._CARD_NOISE_TOKEN.match(token) for token in remainder.split())

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
            candidate
            for candidate in (
                self._anchor_from_raw_item(item) for item in raw_items if item.get("href")
            )
            if candidate is not None
        ]

    def _anchor_from_raw_item(self, item: dict[str, object]) -> AnchorCandidate | None:
        author_name = self._normalize_author_name(str(item.get("author_name", "")))
        title = " ".join(str(item.get("text", "")).split())
        # 标题回退到了作者行时，宁可留空也不要写入伪标题：整段卡片文本
        # 仍然保留在 raw_text 里供品牌匹配使用，而空标题会被质量闸门
        # 统计出来，选择器失效才不会静默通过。
        if self._is_author_echo_title(title, author_name):
            title = ""
        return AnchorCandidate(
            href=str(item.get("href", "")),
            text=title,
            media_kind=str(item.get("media_kind", "")),
            author_name=author_name,
            raw_text=str(item.get("raw_text", "")),
        )
