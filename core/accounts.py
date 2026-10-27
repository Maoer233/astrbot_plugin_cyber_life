"""Account registry and per-account cookie / browser state storage.

Only session material is stored (cookies or a Playwright ``storage_state``);
passwords are never collected because every platform logs in by QR code.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .util import atomic_write_json, iso_now, read_json, safe_filename


PLATFORMS: dict[str, dict[str, str]] = {
    "bilibili": {"label": "B站", "color": "#fb7299", "login": "api_qr"},
    "xiaoheihe": {"label": "小黑盒", "color": "#ffcc00", "login": "browser_qr"},
    "tieba": {"label": "贴吧", "color": "#4e6ef2", "login": "browser_qr"},
}


@dataclass
class Account:
    platform: str
    name: str = "default"
    nickname: str = ""
    uid: str = ""
    status: str = "logged_out"  # logged_out / logged_in / expired
    created_at: str = ""
    last_login_at: str = ""
    last_check_at: str = ""
    cookie_names: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.name}"

    @property
    def label(self) -> str:
        platform = PLATFORMS.get(self.platform, {}).get("label", self.platform)
        return f"{platform} · {self.nickname or self.name}"

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["key"] = self.key
        data["platform_label"] = PLATFORMS.get(self.platform, {}).get("label", self.platform)
        data["color"] = PLATFORMS.get(self.platform, {}).get("color", "#888")
        return data


class AccountStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.path = data_dir / "accounts.json"
        self.sessions_dir = data_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        raw = read_json(self.path, {})
        self.accounts: dict[str, Account] = {}
        self.active: dict[str, str] = {}
        if isinstance(raw, dict):
            for key, value in (raw.get("accounts") or {}).items():
                try:
                    account = Account(**value)
                    if account.platform in PLATFORMS:
                        self.accounts[key] = account
                except (TypeError, ValueError):
                    continue
            self.active = dict(raw.get("active") or {})

    # ------------------------------------------------------------- internal
    def _save(self) -> None:
        atomic_write_json(
            self.path,
            {
                "accounts": {key: asdict(account) for key, account in self.accounts.items()},
                "active": self.active,
                "updated_at": iso_now(),
            },
        )

    def _session_path(self, platform: str, name: str) -> Path:
        return self.sessions_dir / safe_filename(platform) / f"{safe_filename(name, 'default')}.json"

    def storage_state_path(self, platform: str, name: str = "default") -> Path:
        return self.sessions_dir / safe_filename(platform) / f"{safe_filename(name, 'default')}.storage.json"

    # ----------------------------------------------------------------- read
    def get(self, platform: str, name: str = "default") -> Account | None:
        return self.accounts.get(f"{platform}:{name}")

    def active_account(self, platform: str) -> Account | None:
        name = self.active.get(platform)
        if name:
            account = self.get(platform, name)
            if account:
                return account
        candidates = [a for a in self.accounts.values() if a.platform == platform]
        return candidates[0] if candidates else None

    def list(self, platform: str | None = None) -> list[Account]:
        values = sorted(self.accounts.values(), key=lambda a: (a.platform, a.name))
        return [a for a in values if platform is None or a.platform == platform]

    def public_list(self) -> list[dict[str, Any]]:
        return [account.public() for account in self.list()]

    def available_platforms(self) -> list[dict[str, Any]]:
        result = []
        for key, meta in PLATFORMS.items():
            account = self.active_account(key)
            result.append(
                {
                    "platform": key,
                    "label": meta["label"],
                    "color": meta["color"],
                    "login": meta["login"],
                    "logged_in": bool(account and account.status == "logged_in"),
                    "account": account.name if account else "",
                    "nickname": account.nickname if account else "",
                }
            )
        return result

    # ---------------------------------------------------------------- write
    async def upsert(
        self,
        platform: str,
        name: str = "default",
        *,
        nickname: str = "",
        uid: str = "",
        cookie_names: list[str] | None = None,
        meta: dict[str, Any] | None = None,
        status: str = "logged_in",
    ) -> Account:
        if platform not in PLATFORMS:
            raise ValueError(f"不支持的平台：{platform}")
        name = (name or "default").strip()[:32] or "default"
        async with self._lock:
            key = f"{platform}:{name}"
            account = self.accounts.get(key) or Account(platform=platform, name=name, created_at=iso_now())
            account.status = status
            account.last_login_at = iso_now()
            if nickname:
                account.nickname = nickname
            if uid:
                account.uid = str(uid)
            if cookie_names is not None:
                account.cookie_names = cookie_names
            if meta:
                account.meta.update(meta)
            self.accounts[key] = account
            self.active.setdefault(platform, name)
            if account.status == "logged_in":
                self.active[platform] = name
            self._save()
            return account

    async def save_session(self, platform: str, name: str, cookies: dict[str, str], meta: dict | None = None) -> Account:
        path = self._session_path(platform, name)
        atomic_write_json(path, {"cookies": cookies, "saved_at": iso_now(), "meta": meta or {}})
        return await self.upsert(
            platform,
            name,
            cookie_names=sorted(cookies),
            meta=meta,
            status="logged_in",
        )

    async def save_meta(self, platform: str, name: str, nickname: str = "", uid: str = "") -> None:
        async with self._lock:
            account = self.get(platform, name)
            if not account:
                return
            if nickname:
                account.nickname = nickname
            if uid:
                account.uid = str(uid)
            self._save()

    def load_session(self, platform: str, name: str = "default") -> dict[str, Any]:
        return read_json(self._session_path(platform, name), {})

    def cookies(self, platform: str, name: str = "default") -> dict[str, str]:
        return dict(self.load_session(platform, name).get("cookies") or {})

    def has_session(self, platform: str, name: str = "default") -> bool:
        if self.storage_state_path(platform, name).exists():
            return True
        data = self.load_session(platform, name)
        return bool(data.get("cookies"))

    async def set_active(self, platform: str, name: str) -> Account:
        async with self._lock:
            account = self.get(platform, name)
            if not account:
                raise ValueError("账号不存在")
            self.active[platform] = name
            self._save()
            return account

    async def remove(self, platform: str, name: str) -> bool:
        async with self._lock:
            key = f"{platform}:{name}"
            account = self.accounts.pop(key, None)
            if not account:
                return False
            for path in (self._session_path(platform, name), self.storage_state_path(platform, name)):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            if self.active.get(platform) == name:
                remaining = [a.name for a in self.accounts.values() if a.platform == platform]
                if remaining:
                    self.active[platform] = remaining[0]
                else:
                    self.active.pop(platform, None)
            self._save()
            return True

    async def mark_status(self, platform: str, name: str, status: str) -> None:
        async with self._lock:
            account = self.get(platform, name)
            if account:
                account.status = status
                account.last_check_at = iso_now()
                self._save()
