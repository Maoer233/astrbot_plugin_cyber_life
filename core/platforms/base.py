"""Platform-neutral post / comment models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class PlatformError(RuntimeError):
    pass


@dataclass
class Post:
    platform: str
    id: str
    title: str
    url: str = ""
    author: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    duration: float = 0.0
    pic: str = ""
    stats: dict[str, Any] = field(default_factory=dict)
    aid: str = ""
    bvid: str = ""
    cid: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["summary"] = (self.description or self.title)[:160]
        return data


@dataclass
class Reply:
    id: str
    author: str
    text: str
    created_at: str = ""
    is_self: bool = False
    root_id: str = ""
    parent_id: str = ""


@dataclass
class CommentResult:
    ok: bool
    id: str = ""
    message: str = ""
    url: str = ""
    screenshot: str = ""
