"""QR login orchestration for API and browser platforms."""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from astrbot.api import logger
from PIL import Image, ImageDraw, ImageFont

from .accounts import AccountStore, PLATFORMS
from .browser import BrowserError, BrowserLoginHandle, BrowserManager
from .platforms.bilibili import BilibiliClient, BilibiliError
from .platforms.specs import site_spec
from .util import now_tz


@dataclass
class LoginSession:
    id: str
    platform: str
    name: str
    umo: str = ""
    status: str = "waiting"  # waiting / scanned / success / expired / error
    message: str = "等待扫码"
    qr_path: Path | None = None
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    nickname: str = ""
    handle: BrowserLoginHandle | None = None
    qr_key: str = ""

    def public(self, include_image: bool = False) -> dict[str, Any]:
        label = PLATFORMS.get(self.platform, {}).get("label", self.platform)
        data = {
            "id": self.id,
            "platform": self.platform,
            "platform_label": label,
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "nickname": self.nickname,
            "created_at": self.created_at,
            "expires_in": max(0, int(self.expires_at - time.time())),
        }
        if include_image and self.qr_path and self.qr_path.exists():
            try:
                data["image"] = "data:image/png;base64," + base64.b64encode(self.qr_path.read_bytes()).decode()
            except OSError:
                data["image"] = ""
        return data


def _font(size: int) -> ImageFont.ImageFont:
    for path in (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"):
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_qr(url: str, target: Path, caption: str = "") -> Path:
    import qrcode

    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#10131a", back_color="white").convert("RGB")
    if caption:
        width = max(image.width, 340)
        canvas = Image.new("RGB", (width, image.height + 54), "white")
        canvas.paste(image, ((width - image.width) // 2, 0))
        draw = ImageDraw.Draw(canvas)
        font = _font(20)
        bbox = draw.textbbox((0, 0), caption, font=font)
        draw.text(((width - (bbox[2] - bbox[0])) / 2, image.height + 12), caption, font=font, fill="#20242e")
        image = canvas
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, "PNG")
    return target


class LoginManager:
    def __init__(
        self,
        accounts: AccountStore,
        browser: BrowserManager,
        data_dir: Path,
        *,
        send_message: Callable[[str, str, Path | None], Awaitable[None]],
        on_success: Callable[[LoginSession], Awaitable[None]] | None = None,
        timeout: int = 180,
    ) -> None:
        self.accounts = accounts
        self.browser = browser
        self.data_dir = data_dir
        self.qr_dir = data_dir / "media" / "login"
        self.qr_dir.mkdir(parents=True, exist_ok=True)
        self.send_message = send_message
        self.on_success = on_success
        self.timeout = timeout
        self.sessions: dict[str, LoginSession] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def get(self, session_id: str | None = None) -> LoginSession | None:
        if session_id:
            return self.sessions.get(session_id)
        active = [session for session in self.sessions.values() if session.status in ("waiting", "scanned")]
        return active[-1] if active else None

    async def start(self, platform: str, name: str = "default", umo: str = "") -> LoginSession:
        if platform not in PLATFORMS:
            raise ValueError(f"不支持的平台：{platform}")
        # reuse a live session for the same account
        for session in self.sessions.values():
            if session.platform == platform and session.name == name and session.status in ("waiting", "scanned"):
                return session
        session_id = uuid.uuid4().hex[:10]
        session = LoginSession(
            id=session_id,
            platform=platform,
            name=(name or "default").strip()[:32] or "default",
            umo=umo,
            expires_at=time.time() + self.timeout,
        )
        self.sessions[session_id] = session
        if platform == "bilibili":
            await self._start_bilibili(session)
        else:
            await self._start_browser(session)
        self._tasks[session_id] = asyncio.create_task(self._poll_loop(session))
        return session

    async def _start_bilibili(self, session: LoginSession) -> None:
        try:
            qr = await BilibiliClient.create_qr()
            session.qr_key = qr["key"]
            session.qr_path = render_qr(
                qr["url"],
                self.qr_dir / f"bilibili-{session.id}.png",
                "打开 B站 App 扫码登录",
            )
            session.message = "等待扫码（B站 App → 扫一扫）"
        except Exception as exc:  # noqa: BLE001
            session.status = "error"
            session.message = f"生成二维码失败：{exc}"

    async def _start_browser(self, session: LoginSession) -> None:
        spec = site_spec(session.platform)
        if not spec:
            session.status = "error"
            session.message = f"{session.platform} 没有登录配置"
            return
        try:
            handle = await self.browser.begin_login(session.platform, session.name, spec)
            session.handle = handle
            session.qr_path = handle.screenshot
            session.message = f"用 {spec.get('label', session.platform)} App 扫码，扫完可发送 /life confirm {session.id}"
        except Exception as exc:  # noqa: BLE001
            session.status = "error"
            session.message = f"打开登录页失败：{exc}"

    async def _poll_loop(self, session: LoginSession) -> None:
        try:
            while time.time() < session.expires_at:
                if session.status in ("success", "expired", "error"):
                    break
                await asyncio.sleep(2.2)
                if session.platform == "bilibili":
                    await self._poll_bilibili(session)
                else:
                    await self._poll_browser(session)
                if session.status == "success":
                    break
            if session.status in ("waiting", "scanned"):
                session.status = "expired"
                session.message = "二维码已过期，请重新发起登录"
            if session.status == "success":
                await self._notify(
                    session,
                    f"✅ {session.platform} 登录成功：{session.nickname or session.name}\n之后我会自己保存登录状态。",
                )
                if self.on_success:
                    try:
                        await self.on_success(session)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"[cyber-life] 登录成功回调失败：{exc}")
            elif session.status == "expired":
                await self._notify(session, f"⌛ {session.platform} 二维码已过期，发送 /life login {session.platform} 重新获取。")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[cyber-life] 登录轮询异常：{exc}")
            session.status = "error"
            session.message = str(exc)
            await self._notify(session, f"❌ {session.platform} 登录失败：{exc}")
        finally:
            if session.handle:
                await session.handle.close()
                session.handle = None

    async def _poll_bilibili(self, session: LoginSession) -> None:
        try:
            result = await BilibiliClient.poll_qr(session.qr_key)
        except BilibiliError as exc:
            session.message = str(exc)
            return
        status = result.get("status")
        if status == "scanned":
            if session.status != "scanned":
                session.status = "scanned"
                session.message = "已扫码，请在手机上确认"
        elif status == "expired":
            session.status = "expired"
            session.message = "二维码已过期"
        elif status == "success":
            cookies = result.get("cookies") or {}
            if not cookies.get("SESSDATA"):
                session.message = "扫码成功但没有拿到登录 Cookie，请重试"
                return
            client = BilibiliClient(cookies)
            nav = await client.nav()
            if not nav.get("logged_in"):
                session.message = "已扫码，但 B站会话还没生效，稍等…"
                return
            await self.accounts.save_session(
                "bilibili",
                session.name,
                cookies,
                meta={"nickname": nav.get("nickname", ""), "uid": nav.get("uid", ""), "source": "qr"},
            )
            session.status = "success"
            session.nickname = str(nav.get("nickname") or "")
            session.message = f"登录成功：{session.nickname or session.name}"

    async def _poll_browser(self, session: LoginSession) -> None:
        if not session.handle:
            return
        spec = site_spec(session.platform)
        if await self.browser.is_logged_in(session.handle, spec):
            await self._finish_browser(session)

    async def _finish_browser(self, session: LoginSession) -> None:
        if not session.handle:
            session.status = "error"
            session.message = "登录窗口已关闭"
            return
        storage_path = self.accounts.storage_state_path(session.platform, session.name)
        await self.browser.save_storage(session.handle, storage_path)
        cookies = await session.handle.cookies()
        await self.accounts.save_session(session.platform, session.name, cookies)
        session.status = "success"
        session.message = "登录成功，登录态已保存"
        await session.handle.close()
        session.handle = None

    async def confirm(self, session_id: str) -> LoginSession | None:
        session = self.sessions.get(session_id)
        if not session:
            return None
        if session.platform == "bilibili":
            session.message = "B站登录需要等待扫码接口确认，请稍等"
            return session
        if session.handle:
            await self._finish_browser(session)
        return session

    async def cancel(self, session_id: str) -> bool:
        session = self.sessions.get(session_id)
        if not session:
            return False
        task = self._tasks.get(session_id)
        if task and not task.done():
            task.cancel()
        if session.handle:
            await session.handle.close()
            session.handle = None
        session.status = "expired"
        session.message = "已取消"
        return True

    async def close(self) -> None:
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for session in self.sessions.values():
            if session.handle:
                await session.handle.close()
                session.handle = None

    async def _notify(self, session: LoginSession, text: str) -> None:
        if not session.umo:
            return
        try:
            await self.send_message(session.umo, text, None)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[cyber-life] 登录通知发送失败：{exc}")
