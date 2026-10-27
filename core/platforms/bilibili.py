"""Bilibili adapter.

Login and commenting use Bilibili's own web APIs rather than DOM automation:
the QR login flow is stable, and cookies are the only state that has to be
persisted.  Browsing uses the public popular/ranking feed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from .base import CommentResult, PlatformError, Post, Reply


API = "https://api.bilibili.com"
PASSPORT = "https://passport.bilibili.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
}


class BilibiliError(PlatformError):
    pass


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class BilibiliClient:
    def __init__(self, cookies: dict[str, str] | None = None, timeout: float = 25) -> None:
        self.cookies = dict(cookies or {})
        self.timeout = timeout

    # ------------------------------------------------------------------ http
    def _client(self, cookies: dict[str, str] | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers=HEADERS,
            cookies=cookies if cookies is not None else self.cookies,
        )

    async def _json(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        headers: dict | None = None,
    ) -> dict:
        request_headers = dict(HEADERS)
        if headers:
            request_headers.update(headers)
        async with self._client() as client:
            response = await client.request(
                method, url, params=params, data=data, headers=request_headers
            )
        if response.status_code >= 400:
            raise BilibiliError(f"B站接口 HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise BilibiliError("B站接口返回了非 JSON 内容（可能需要重新登录）") from exc
        if not isinstance(payload, dict):
            raise BilibiliError("B站接口返回格式异常")
        return payload

    # ------------------------------------------------------------- QR login
    @staticmethod
    async def create_qr() -> dict[str, str]:
        async with httpx.AsyncClient(timeout=20, headers=HEADERS) as client:
            response = await client.get(
                f"{PASSPORT}/x/passport-login/web/qrcode/generate",
                params={"source": "main-fe-header"},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise BilibiliError("获取登录二维码失败：接口没有返回 JSON") from exc
        data = payload.get("data") or {}
        if payload.get("code") != 0 or not data.get("url"):
            raise BilibiliError(f"获取登录二维码失败：{payload.get('message') or payload.get('code')}")
        return {"url": data["url"], "key": data.get("qrcode_key", "")}

    @staticmethod
    async def poll_qr(key: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
            response = await client.get(
                f"{PASSPORT}/x/passport-login/web/qrcode/poll",
                params={"qrcode_key": key, "source": "main-fe-header"},
            )
            cookies = {cookie.name: cookie.value for cookie in client.cookies.jar}
        try:
            payload = response.json()
        except ValueError as exc:
            raise BilibiliError("轮询二维码状态失败") from exc
        data = payload.get("data") or {}
        code = data.get("code")
        if code == 0:
            status = "success"
        elif code == 86101:
            status = "waiting"
        elif code == 86090:
            status = "scanned"
        elif code == 86038:
            status = "expired"
        else:
            status = "waiting"
        return {
            "status": status,
            "code": code,
            "message": data.get("message") or payload.get("message", ""),
            "cookies": cookies,
            "url": data.get("url", ""),
        }

    async def nav(self) -> dict[str, Any]:
        payload = await self._json("GET", f"{API}/x/web-interface/nav")
        data = payload.get("data") or {}
        return {
            "logged_in": bool(data.get("isLogin")),
            "nickname": data.get("uname", ""),
            "uid": str(data.get("mid", "")),
            "vip": data.get("vipStatus"),
            "level": data.get("level_info", {}).get("current_level"),
        }

    # ------------------------------------------------------------------ feed
    async def popular(self, limit: int = 12) -> list[Post]:
        payload = await self._json(
            "GET", f"{API}/x/web-interface/popular", params={"ps": max(1, min(limit, 50)), "pn": 1}
        )
        if payload.get("code") != 0:
            raise BilibiliError(f"获取B站热门失败：{payload.get('message')}")
        posts = []
        for item in (payload.get("data") or {}).get("list") or []:
            posts.append(self._post_from_feed(item))
        if not posts:
            payload = await self._json(
                "GET", f"{API}/x/web-interface/ranking/v2", params={"rid": 0, "type": "all"}
            )
            for item in (payload.get("data") or {}).get("list") or []:
                posts.append(self._post_from_feed(item))
        return posts[:limit]

    def _post_from_feed(self, item: dict) -> Post:
        owner = item.get("owner") or {}
        stat = item.get("stat") or {}
        duration = item.get("duration")
        if isinstance(duration, str) and ":" in duration:
            parts = [int(p) for p in duration.split(":") if p.isdigit()]
            duration = sum(value * 60 ** (len(parts) - 1 - i) for i, value in enumerate(parts))
        return Post(
            platform="bilibili",
            id=str(item.get("bvid") or item.get("aid") or ""),
            title=str(item.get("title") or "").strip(),
            url=f"https://www.bilibili.com/video/{item.get('bvid')}" if item.get("bvid") else "",
            author=str(owner.get("name") or ""),
            description=str(item.get("desc") or "").strip(),
            tags=[str(item.get("tname") or "")] if item.get("tname") else [],
            duration=float(duration or 0),
            pic=str(item.get("pic") or ""),
            stats={
                "view": _as_int(stat.get("view")),
                "like": _as_int(stat.get("like")),
                "danmaku": _as_int(stat.get("danmaku")),
                "reply": _as_int(stat.get("reply")),
            },
            aid=str(item.get("aid") or ""),
            bvid=str(item.get("bvid") or ""),
            cid=str(item.get("cid") or ""),
        )

    async def video_detail(self, bvid: str) -> Post:
        payload = await self._json("GET", f"{API}/x/web-interface/view", params={"bvid": bvid})
        if payload.get("code") != 0:
            raise BilibiliError(f"获取视频信息失败：{payload.get('message')}")
        data = payload.get("data") or {}
        pages = data.get("pages") or [{}]
        stat = data.get("stat") or {}
        return Post(
            platform="bilibili",
            id=str(data.get("bvid") or bvid),
            title=str(data.get("title") or ""),
            url=f"https://www.bilibili.com/video/{data.get('bvid') or bvid}",
            author=str((data.get("owner") or {}).get("name") or ""),
            description=str(data.get("desc") or ""),
            tags=[str(t.get("tag_name")) for t in (data.get("tags") or []) if isinstance(t, dict)],
            duration=float(data.get("duration") or 0),
            pic=str(data.get("pic") or ""),
            stats={
                "view": _as_int(stat.get("view")),
                "like": _as_int(stat.get("like")),
                "danmaku": _as_int(stat.get("danmaku")),
                "reply": _as_int(stat.get("reply")),
            },
            aid=str(data.get("aid") or ""),
            bvid=str(data.get("bvid") or bvid),
            cid=str((pages[0] or {}).get("cid") or ""),
            extra={"pages": len(pages), "pubdate": data.get("pubdate")},
        )

    async def fetch_subtitle(self, post: Post) -> dict[str, Any] | None:
        """Bilibili's AI/human subtitle track, as timed segments.

        The player endpoint returns an authenticated ``subtitle_url``.  Many
        videos (especially ones published recently) have an ``ai-zh`` track;
        those are much more accurate than local ASR for names and jargon.
        """
        bvid = post.bvid
        cid = post.cid or str(post.extra.get("cid") or "")
        if not bvid or not cid:
            return None
        payload = await self._json(
            "GET",
            f"{API}/x/player/wbi/v2",
            params={"bvid": bvid, "cid": cid},
        )
        if payload.get("code") != 0:
            raise BilibiliError(f"读取字幕信息失败：{payload.get('message')}")
        subtitle = ((payload.get("data") or {}).get("subtitle") or {})
        tracks = subtitle.get("subtitles") or []
        if not isinstance(tracks, list):
            return None
        from ..transcribe import pick_track

        track = pick_track(tracks)
        if not track:
            return None
        url = str(track.get("subtitle_url") or "")
        if url.startswith("//"):
            url = "https:" + url
        async with self._client() as client:
            response = await client.get(url)
        if response.status_code >= 400:
            raise BilibiliError(f"下载字幕失败：HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise BilibiliError("字幕文件不是 JSON") from exc
        segments = []
        for item in body.get("body") or []:
            text = str(item.get("content") or "").strip()
            if not text:
                continue
            try:
                start = float(item.get("from") or 0)
                end = float(item.get("to") or start)
            except (TypeError, ValueError):
                start, end = 0.0, 0.0
            segments.append({"start": start, "end": end, "text": text})
        if not segments:
            return None
        return {
            "language": str(track.get("lan") or ""),
            "label": str(track.get("lan_doc") or ""),
            "segments": segments,
        }

    async def play_url(self, bvid: str, cid: str) -> str:
        params = {"bvid": bvid, "cid": cid, "qn": 64, "fnval": 1, "fourk": 0}
        payload = await self._json("GET", f"{API}/x/player/playurl", params=params)
        if payload.get("code") != 0:
            raise BilibiliError(f"获取视频流失败：{payload.get('message')}")
        data = payload.get("data") or {}
        durl = data.get("durl") or []
        if durl and durl[0].get("url"):
            return str(durl[0]["url"])
        dash = data.get("dash") or {}
        videos = dash.get("video") or []
        if videos:
            return str(videos[0].get("baseUrl") or videos[0].get("base_url") or "")
        return ""

    async def download(self, url: str, target: Path, max_bytes: int = 220 * 1024 * 1024) -> Path:
        if not url:
            raise BilibiliError("视频直链为空")
        target.parent.mkdir(parents=True, exist_ok=True)
        headers = dict(HEADERS)
        headers["Referer"] = "https://www.bilibili.com/"
        async with httpx.AsyncClient(timeout=90, headers=headers, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise BilibiliError(f"下载视频失败：HTTP {response.status_code}")
                written = 0
                with target.open("wb") as fh:
                    async for chunk in response.aiter_bytes(256 * 1024):
                        written += len(chunk)
                        if written > max_bytes:
                            raise BilibiliError("视频超过大小限制，已跳过")
                        fh.write(chunk)
        return target

    # --------------------------------------------------------------- comments
    @property
    def csrf(self) -> str:
        return self.cookies.get("bili_jct", "")

    async def add_comment(self, post: Post, message: str) -> CommentResult:
        if not self.cookies.get("SESSDATA"):
            return CommentResult(ok=False, message="B站未登录或登录已过期")
        if not self.csrf:
            return CommentResult(ok=False, message="缺少 bili_jct，请重新扫码登录")
        oid = post.aid or post.extra.get("aid", "")
        if not oid:
            return CommentResult(ok=False, message="缺少视频 aid")
        payload = await self._json(
            "POST",
            f"{API}/x/v2/reply/add",
            data={
                "oid": oid,
                "type": 1,
                "message": message,
                "plat": 1,
                "csrf": self.csrf,
            },
            headers={"Referer": post.url or "https://www.bilibili.com/"},
        )
        if payload.get("code") != 0:
            return CommentResult(ok=False, message=str(payload.get("message") or "评论失败"))
        data = payload.get("data") or {}
        rpid = str((data.get("rpid") or (data.get("reply") or {}).get("rpid") or ""))
        return CommentResult(ok=True, id=rpid, message="评论成功", url=post.url)

    async def reply_comment(self, post: Post, root_id: str, parent_id: str, message: str) -> CommentResult:
        if not self.csrf:
            return CommentResult(ok=False, message="缺少 bili_jct，请重新扫码登录")
        oid = post.aid or post.extra.get("aid", "")
        payload = await self._json(
            "POST",
            f"{API}/x/v2/reply/add",
            data={
                "oid": oid,
                "type": 1,
                "message": message,
                "plat": 1,
                "root": root_id,
                "parent": parent_id or root_id,
                "csrf": self.csrf,
            },
            headers={"Referer": post.url or "https://www.bilibili.com/"},
        )
        if payload.get("code") != 0:
            return CommentResult(ok=False, message=str(payload.get("message") or "回复失败"))
        data = payload.get("data") or {}
        return CommentResult(
            ok=True,
            id=str((data.get("rpid") or (data.get("reply") or {}).get("rpid") or "")),
            message="回复成功",
            url=post.url,
        )

    async def thread_replies(self, post: Post, root_id: str) -> list[Reply]:
        oid = post.aid or post.extra.get("aid", "")
        payload = await self._json(
            "GET",
            f"{API}/x/v2/reply/reply",
            params={"type": 1, "oid": oid, "root": root_id, "pn": 1, "ps": 20},
        )
        if payload.get("code") != 0:
            return []
        replies = []
        my_uid = self.cookies.get("DedeUserID", "")
        for item in (payload.get("data") or {}).get("replies") or []:
            member = item.get("member") or {}
            content = item.get("content") or {}
            mid = str(member.get("mid", ""))
            replies.append(
                Reply(
                    id=str(item.get("rpid") or ""),
                    author=str(member.get("uname") or ""),
                    text=str(content.get("message") or ""),
                    created_at=str(item.get("ctime") or ""),
                    is_self=bool(my_uid and mid == my_uid),
                    root_id=root_id,
                    parent_id=str(item.get("parent") or ""),
                )
            )
        return replies

    async def my_comments(self, post: Post) -> list[Reply]:
        """Comments under a video authored by the logged-in account."""
        return [item for item in await self.thread_comments(post, limit=20) if item.is_self]

    async def thread_comments(self, post: Post, limit: int = 8) -> list[Reply]:
        """Top-level comments under a video, including our own."""
        oid = post.aid or post.extra.get("aid", "")
        if not oid:
            return []
        payload = await self._json(
            "GET",
            f"{API}/x/v2/reply",
            params={"type": 1, "oid": oid, "pn": 1, "ps": max(1, min(20, limit)), "sort": 2},
        )
        if payload.get("code") != 0:
            return []
        my_uid = self.cookies.get("DedeUserID", "")
        result = []
        for item in (payload.get("data") or {}).get("replies") or []:
            member = item.get("member") or {}
            mid = str(member.get("mid", ""))
            result.append(
                Reply(
                    id=str(item.get("rpid") or ""),
                    author=str(member.get("uname") or ""),
                    text=str((item.get("content") or {}).get("message") or ""),
                    created_at=str(item.get("ctime") or ""),
                    is_self=bool(my_uid and mid == my_uid),
                )
            )
        return result


class BilibiliPlatform:
    """Small facade used by the activity engine."""

    name = "bilibili"
    label = "B站"

    def __init__(self, accounts, account_name: str = "default") -> None:
        self.accounts = accounts
        self.account_name = account_name

    def client(self) -> BilibiliClient:
        return BilibiliClient(self.accounts.cookies("bilibili", self.account_name))

    async def feed(self, limit: int = 10) -> list[Post]:
        return await self.client().popular(limit)

    async def detail(self, post: Post) -> Post:
        if post.bvid and not post.cid:
            post = await self.client().video_detail(post.bvid)
        return post

    async def comment(self, post: Post, text: str) -> CommentResult:
        return await self.client().add_comment(post, text)

    async def replies(self, post: Post, root_id: str) -> list[Reply]:
        return await self.client().thread_replies(post, root_id)

    async def comments(self, post: Post, limit: int = 8) -> list[Reply]:
        return await self.client().thread_comments(post, limit)

    async def reply(self, post: Post, root_id: str, parent_id: str, text: str) -> CommentResult:
        return await self.client().reply_comment(post, root_id, parent_id, text)
