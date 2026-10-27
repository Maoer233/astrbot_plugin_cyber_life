"""Browser-driven platforms (Tieba, Xiaoheihe).

No stable public API is used here.  The adapter opens the site with the saved
Playwright storage state, scrapes feed cards, and comments through the page's
own input box.  All failures are reported instead of guessed.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urljoin

from astrbot.api import logger

from ..browser import BrowserManager, BrowserError
from .base import CommentResult, PlatformError, Post, Reply
from .specs import site_spec


class BrowserPlatform:
    def __init__(
        self,
        platform: str,
        accounts,
        browser: BrowserManager,
        account_name: str | None = None,
    ) -> None:
        self.platform = platform
        self.accounts = accounts
        self.browser = browser
        self.account_name = account_name or (
            accounts.active_account(platform).name if accounts.active_account(platform) else "default"
        )
        self.spec = site_spec(platform)
        if not self.spec:
            raise PlatformError(f"没有 {platform} 的站点配置")
        self.label = self.spec.get("label", platform)
        self.supports_reply = False

    def _post_id(self, url: str) -> str:
        return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:16]

    async def feed(self, limit: int = 10) -> list[Post]:
        result = await self.browser.browse(
            self.platform,
            self.account_name,
            self.spec["feed_url"],
            self.spec,
        )
        posts: list[Post] = []
        seen = set()
        for card in result.get("cards", []):
            title = (card.get("title") or "").strip()
            href = (card.get("url") or "").strip()
            if not title or not href:
                continue
            url = urljoin(self.spec["feed_url"], href)
            if url in seen:
                continue
            seen.add(url)
            posts.append(
                Post(
                    platform=self.platform,
                    id=self._post_id(url),
                    title=title[:180],
                    url=url,
                    description=(card.get("body") or "")[:400],
                    extra={"screenshot": result.get("screenshot", "")},
                )
            )
            if len(posts) >= limit:
                break
        return posts

    async def detail(self, post: Post) -> Post:
        return post

    async def comment(self, post: Post, text: str) -> CommentResult:
        try:
            result = await self.browser.post_comment(
                self.platform,
                self.account_name,
                post.url,
                text,
                self.spec,
            )
        except BrowserError as exc:
            return CommentResult(ok=False, message=str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[cyber-life] {self.platform} 评论异常：{exc}")
            return CommentResult(ok=False, message=f"评论异常：{exc}")
        return CommentResult(
            ok=bool(result.get("ok")),
            message=str(result.get("message") or ""),
            url=post.url,
            screenshot=str(result.get("screenshot") or ""),
        )

    async def replies(self, post: Post, root_id: str) -> list[Reply]:
        return []

    async def reply(self, post: Post, root_id: str, parent_id: str, text: str) -> CommentResult:
        return CommentResult(ok=False, message=f"{self.label} 暂不支持自动回复评论")
