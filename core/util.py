"""Small, dependency-free helpers shared by the plugin."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


DEFAULT_TZ = "Asia/Shanghai"


def tzinfo(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except Exception:  # noqa: BLE001 - bad user input must not crash the bot
        return ZoneInfo(DEFAULT_TZ)


def now_tz(name: str | None = None) -> datetime:
    return datetime.now(tzinfo(name))


def today_key(dt: datetime | None = None) -> str:
    return (dt or now_tz()).strftime("%Y-%m-%d")


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically so a crash never leaves a half-written state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def safe_filename(value: str, fallback: str = "item") -> str:
    value = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", value or "")
    value = value.strip("._-")
    return value[:80] or fallback


def human_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒" if sec else f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def parse_hhmm(value: str) -> int:
    """Return minutes since midnight for ``HH:MM``."""
    text = (value or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        raise ValueError(f"时间格式应为 HH:MM：{value!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时间超出范围：{value!r}")
    return hour * 60 + minute


def format_minutes(minutes: int) -> str:
    if minutes == 24 * 60:
        return "24:00"
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def iso_now(tz_name: str | None = None) -> str:
    return now_tz(tz_name).isoformat(timespec="seconds")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def shorten(text: str, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= limit else text[: limit - 1] + "…"
