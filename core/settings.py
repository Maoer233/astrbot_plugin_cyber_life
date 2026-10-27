"""Runtime settings: plugin config + a small persisted override layer.

The WebUI plugin page writes to ``settings.json`` so schedules and frequencies
can be changed without asking the user to edit the plugin config and reload.
Values in the override file win over ``_conf_schema.json`` defaults.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .holidays import HolidayCalendar
from .schedule import ACCESS_MODES, AutoSchedule, Phase
from .util import atomic_write_json, read_json


DEFAULTS: dict[str, Any] = {
    # master switches
    "enabled": True,
    "auto_run": True,
    # Standing permission for the bot's own tools and scheduled activity.
    # Chat auto-trigger and prompt injection are intentionally off by default:
    # ordinary conversation must never be hijacked by keyword/classifier matches.
    "agent_autonomy": False,
    "chat_auto_trigger": False,
    "auto_login": True,
    "agent_manage_usage": True,
    "agent_router_timeout": 8,
    # time budget / automatic peak-valley access
    "timezone": "Asia/Shanghai",
    "access_mode": "all_day",
    "peak_frequency_percent": 40,
    "holiday_enabled": True,
    "extra_holiday_dates": [],
    "extra_workday_dates": [],
    "daily_watch_minutes": 60,
    "daily_watch_budget_is_total": True,
    # commenting
    "comment_enabled": True,
    "comment_interval_min": 35,
    "comment_interval_max": 90,
    "comment_probability": 0.55,
    "max_comments_per_day": 8,
    "comment_max_chars": 80,
    "reply_enabled": True,
    "reply_interval_min": 30,
    "reply_interval_max": 75,
    "max_replies_per_day": 6,
    "reply_max_chars": 60,
    # platforms / idle content
    "platforms": ["bilibili", "xiaoheihe", "tieba"],
    "browse_interval_min": 12,
    "browse_interval_max": 35,
    "video_min_frames": 10,
    "video_max_frames": 20,
    "video_max_seconds": 240,
    "ffmpeg_path": "",
    "vision_enabled": True,
    "vision_provider_umo": "",
    # speech-to-text: Bilibili's own AI subtitle first, local Whisper fallback
    "subtitle_enabled": True,
    "subtitle_model": "small",
    "subtitle_language": "zh",
    "subtitle_max_seconds": 300,
    "subtitle_hf_endpoint": "https://hf-mirror.com",
    # drawing
    "comfyui_enabled": True,
    "comfyui_url": "http://127.0.0.1:8188",
    "comfyui_workflow": "",
    "comfyui_checkpoint": "v1-5-pruned-emaonly.safetensors",
    "comfyui_width": 512,
    "comfyui_height": 768,
    "comfyui_steps": 24,
    "comfyui_cfg": 7.0,
    "comfyui_sampler": "euler",
    "comfyui_scheduler": "normal",
    "comfyui_negative": "lowres, bad anatomy, bad hands, text, watermark, blurry",
    "comfyui_timeout": 180,
    "max_draws_per_day": 3,
    "draw_interval_min": 120,
    "draw_interval_max": 240,
    # mood
    "mood_enabled": True,
    "mood_chat_weight": 0.5,
    "mood_decay_per_hour": 0.06,
    "mood_use_llm": False,
    # browser / login
    "browser_enabled": True,
    "browser_headless": True,
    "browser_channel": "msedge",
    "browser_executable": "",
    "browser_timeout_seconds": 45,
    "browser_viewport_width": 1100,
    "browser_viewport_height": 820,
    "login_timeout_seconds": 180,
    # notifications
    "notify_umo": "",
    "notify_actions": False,
    "notify_draw": True,
    # Keep chat able to see what the bot actually did. The activity log is
    # factual context, not a separate outbound notification channel.
    "activity_private": False,
    "keep_activity_days": 14,
    # optional override for proxies that expose the DeepSeek balance endpoint
    "deepseek_balance_base_url": "",
    # access
    "admin_only": True,
    "allow_qq": [],
}

_ENUMS = {
    "access_mode": set(ACCESS_MODES),
    "comfyui_sampler": {"euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde", "ddim", "uni_pc"},
    "comfyui_scheduler": {"normal", "karras", "exponential", "sgm_uniform", "simple", "ddim_uniform"},
    "subtitle_model": {"tiny", "base", "small", "medium", "large-v3"},
    "subtitle_language": {"auto", "zh", "en", "ja"},
}

_RANGES: dict[str, tuple[float, float]] = {
    "comment_interval_min": (1, 1440),
    "comment_interval_max": (1, 2880),
    "reply_interval_min": (1, 1440),
    "reply_interval_max": (1, 2880),
    "max_comments_per_day": (0, 200),
    "max_replies_per_day": (0, 200),
    "daily_watch_minutes": (0, 1440),
    "peak_frequency_percent": (5, 100),
    "agent_router_timeout": (2, 30),
    "browse_interval_min": (1, 1440),
    "browse_interval_max": (1, 2880),
    "video_min_frames": (1, 40),
    "video_max_frames": (1, 60),
    "video_max_seconds": (5, 3600),
    "subtitle_max_seconds": (15, 1800),
    "comfyui_width": (128, 2048),
    "comfyui_height": (128, 2048),
    "comfyui_steps": (1, 150),
    "comfyui_cfg": (0, 30),
    "comfyui_timeout": (10, 900),
    "max_draws_per_day": (0, 50),
    "draw_interval_min": (5, 2880),
    "draw_interval_max": (5, 5760),
    "comment_probability": (0, 1),
    "mood_chat_weight": (0, 3),
    "mood_decay_per_hour": (0, 2),
    "browser_timeout_seconds": (5, 300),
    "login_timeout_seconds": (30, 600),
    "browser_viewport_width": (640, 2560),
    "browser_viewport_height": (480, 1600),
    "keep_activity_days": (1, 365),
}

_BOOL_FIELDS = {key for key, value in DEFAULTS.items() if isinstance(value, bool)}


def _coerce(key: str, value: Any) -> Any:
    default = DEFAULTS.get(key)
    if key in _BOOL_FIELDS:
        return bool(value)
    if key in _RANGES:
        number = float(value)
        low, high = _RANGES[key]
        if not low <= number <= high:
            raise ValueError(f"{key} 必须在 {low:g} 到 {high:g} 之间")
        return int(number) if isinstance(default, int) else round(number, 3)
    if key in _ENUMS:
        if value not in _ENUMS[key]:
            raise ValueError(f"{key} 不支持 {value!r}")
        return value
    if isinstance(default, list):
        if isinstance(value, str):
            value = [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]
        if not isinstance(value, list):
            raise ValueError(f"{key} 需要列表")
        return [str(item) for item in value]
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    if isinstance(default, str):
        return str(value)
    return value


class SettingsStore:
    def __init__(self, plugin_config: dict | None, data_dir: Path) -> None:
        self.path = data_dir / "settings.json"
        self.base = copy.deepcopy(DEFAULTS)
        for key, value in (plugin_config or {}).items():
            if key in self.base:
                try:
                    self.base[key] = _coerce(key, value)
                except (TypeError, ValueError):
                    continue
        self.overrides: dict[str, Any] = {}
        stored = read_json(self.path, {})
        if isinstance(stored, dict):
            for key, value in stored.items():
                if key in DEFAULTS:
                    try:
                        self.overrides[key] = _coerce(key, value)
                    except (TypeError, ValueError):
                        continue
        self.holidays = HolidayCalendar(data_dir)
        self._apply_holiday_settings()

    def _apply_holiday_settings(self) -> None:
        self.holidays.set_enabled(self.bool("holiday_enabled"))
        self.holidays.set_custom(
            self.list("extra_holiday_dates"),
            self.list("extra_workday_dates"),
        )

    async def refresh_holidays(self) -> dict[int, bool]:
        result = await self.holidays.refresh_for_now()
        self._apply_holiday_settings()
        return result

    # ------------------------------------------------------------------ get
    def get(self, key: str, default: Any = None) -> Any:
        if key in self.overrides:
            return self.overrides[key]
        if key in self.base:
            return self.base[key]
        return default

    def int(self, key: str) -> int:
        return int(self.get(key, DEFAULTS.get(key, 0)) or 0)

    def float(self, key: str) -> float:
        return float(self.get(key, DEFAULTS.get(key, 0.0)) or 0.0)

    def bool(self, key: str) -> bool:
        return bool(self.get(key, DEFAULTS.get(key, False)))

    def str(self, key: str) -> str:
        return str(self.get(key, DEFAULTS.get(key, "")) or "")

    def list(self, key: str) -> list:
        value = self.get(key, DEFAULTS.get(key, []))
        return list(value or [])

    def all(self) -> dict[str, Any]:
        merged = copy.deepcopy(self.base)
        merged.update(self.overrides)
        return merged

    # ----------------------------------------------------------------- save
    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ValueError("设置必须是 JSON 对象")
        clean: dict[str, Any] = {}
        for key, value in patch.items():
            if key not in DEFAULTS:
                continue
            clean[key] = _coerce(key, value)

        def merged(name: str):
            return clean[name] if name in clean else self.get(name)

        if merged("comment_interval_max") < merged("comment_interval_min"):
            raise ValueError("评论最大间隔不能小于最小间隔")
        if merged("reply_interval_max") < merged("reply_interval_min"):
            raise ValueError("回复最大间隔不能小于最小间隔")
        if merged("browse_interval_max") < merged("browse_interval_min"):
            raise ValueError("刷帖最大间隔不能小于最小间隔")
        if merged("draw_interval_max") < merged("draw_interval_min"):
            raise ValueError("画画最大间隔不能小于最小间隔")
        if merged("video_max_frames") < merged("video_min_frames"):
            raise ValueError("截图最大张数不能小于最小张数")
        self.overrides.update(clean)
        if any(key in clean for key in ("holiday_enabled", "extra_holiday_dates", "extra_workday_dates")):
            self._apply_holiday_settings()
        atomic_write_json(self.path, self.overrides)
        return clean

    def reset(self) -> None:
        self.overrides = {}
        atomic_write_json(self.path, {})

    # ------------------------------------------------------------- schedule
    def schedule(self) -> AutoSchedule:
        return AutoSchedule(
            access_mode=self.str("access_mode"),
            peak_frequency_percent=self.int("peak_frequency_percent"),
            is_off_day=self.holidays.is_off_day,
        )

    def phase(self, dt) -> Phase:
        return self.schedule().phase(dt)

    def can_call(self, dt) -> bool:
        return self.schedule().can_call(dt)

    def interval_multiplier(self, dt) -> float:
        return self.schedule().interval_multiplier(dt)

    def public(self, now=None) -> dict[str, Any]:
        data = self.all()
        if now is not None:
            schedule = self.schedule()
            data["_phase"] = schedule.phase(now).value
            data["_phase_label"] = schedule.phase(now).label
            data["_active_now"] = schedule.can_act(now)
            data["_access_mode_label"] = ACCESS_MODES[schedule.access_mode]
            data["_holiday"] = self.holidays.status(now)
        return data
