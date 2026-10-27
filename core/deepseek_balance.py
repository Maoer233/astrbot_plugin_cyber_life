"""DeepSeek account balance lookup.

The key is read from AstrBot's active chat provider at runtime and is never
returned to the WebUI or written into plugin data.  Only the public balance
fields are exposed.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


DEFAULT_BASE_URL = "https://api.deepseek.com"
BALANCE_PATH = "/user/balance"


@dataclass
class BalanceInfo:
    currency: str = ""
    total_balance: str = ""
    granted_balance: str = ""
    topped_up_balance: str = ""

    def public(self) -> dict[str, str]:
        return {
            "currency": self.currency,
            "total_balance": self.total_balance,
            "granted_balance": self.granted_balance,
            "topped_up_balance": self.topped_up_balance,
        }


@dataclass
class BalanceResult:
    ok: bool = False
    is_available: bool = False
    balances: list[BalanceInfo] = field(default_factory=list)
    error: str = ""
    fetched_at: str = ""
    cached: bool = False
    source: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "is_available": self.is_available,
            "balances": [item.public() for item in self.balances],
            "error": self.error,
            "fetched_at": self.fetched_at,
            "cached": self.cached,
            "source": self.source,
        }


def _normalize_base_url(value: str) -> str:
    base = (value or "").strip().rstrip("/")
    if not base:
        return DEFAULT_BASE_URL
    if base.endswith("/v1"):
        base = base[:-3]
    return base or DEFAULT_BASE_URL


def _http_get(url: str, key: str, timeout: float = 12) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "AstrBot-CyberLife/1.2",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(1024 * 256).decode("utf-8", errors="replace")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("DeepSeek 返回了非 JSON 对象")
    return data


class DeepSeekBalance:
    """Small cached client for ``GET /user/balance``."""

    def __init__(self, settings, llm) -> None:
        self.settings = settings
        self.llm = llm
        self._cache: BalanceResult | None = None
        self._cache_at = 0.0

    def _provider_config(self) -> dict[str, Any]:
        provider = self.llm.provider()
        if provider is None:
            return {}
        config = getattr(provider, "provider_config", None)
        return config if isinstance(config, dict) else {}

    def _credentials(self) -> tuple[str, str]:
        """Return ``(api_key, base_url)`` without exposing the key."""
        provider = self.llm.provider()
        if provider is None:
            return "", ""
        config = self._provider_config()
        identity = " ".join(
            str(config.get(name) or "")
            for name in ("id", "provider", "provider_source_id", "model", "api_base")
        ).lower()
        if "deepseek" not in identity:
            return "", ""
        key = ""
        try:
            current = provider.get_current_key()
            if isinstance(current, str):
                key = current.strip()
        except Exception:  # noqa: BLE001
            key = ""
        if not key:
            keys = config.get("key") or []
            if isinstance(keys, str):
                keys = [keys]
            if isinstance(keys, list):
                key = next((str(item).strip() for item in keys if item), "")
        base = self.settings.str("deepseek_balance_base_url") or str(config.get("api_base") or "")
        return key, _normalize_base_url(base)

    async def fetch(self, *, force: bool = False) -> BalanceResult:
        now = time.time()
        if (
            not force
            and self._cache is not None
            and now - self._cache_at < 60
        ):
            cached = BalanceResult(**self._cache.__dict__)
            cached.cached = True
            return cached

        key, base = self._credentials()
        if not key:
            result = BalanceResult(
                ok=False,
                error="没有找到 DeepSeek API Key。请确认当前聊天模型使用 DeepSeek，或在设置里填写余额接口地址。",
            )
            return result

        url = f"{base}{BALANCE_PATH}"
        try:
            data = await asyncio.to_thread(_http_get, url, key)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(2048).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                detail = ""
            result = BalanceResult(
                ok=False,
                error=f"DeepSeek 余额接口返回 HTTP {exc.code}：{detail[:180] or exc.reason}",
            )
            return result
        except Exception as exc:  # noqa: BLE001
            result = BalanceResult(ok=False, error=f"读取 DeepSeek 余额失败：{exc}")
            return result

        infos: list[BalanceInfo] = []
        for item in data.get("balance_infos") or []:
            if not isinstance(item, dict):
                continue
            infos.append(
                BalanceInfo(
                    currency=str(item.get("currency") or ""),
                    total_balance=str(item.get("total_balance") or ""),
                    granted_balance=str(item.get("granted_balance") or ""),
                    topped_up_balance=str(item.get("topped_up_balance") or ""),
                )
            )
        result = BalanceResult(
            ok=True,
            is_available=bool(data.get("is_available")),
            balances=infos,
            fetched_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            source=url,
        )
        self._cache = result
        self._cache_at = now
        return result
