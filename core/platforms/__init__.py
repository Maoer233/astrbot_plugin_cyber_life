"""Platform adapter factory."""

from __future__ import annotations

from .base import CommentResult, PlatformError, Post, Reply
from .bilibili import BilibiliClient, BilibiliPlatform
from .browser_sites import BrowserPlatform


def make_platform(name: str, accounts, browser):
    if name == "bilibili":
        account = accounts.active_account("bilibili")
        return BilibiliPlatform(accounts, account.name if account else "default")
    if name in ("tieba", "xiaoheihe"):
        return BrowserPlatform(name, accounts, browser)
    raise PlatformError(f"未知平台：{name}")


__all__ = [
    "BilibiliClient",
    "BilibiliPlatform",
    "BrowserPlatform",
    "CommentResult",
    "PlatformError",
    "Post",
    "Reply",
    "make_platform",
]
