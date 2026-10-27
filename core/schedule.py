"""Automatic peak / valley detection.

The user no longer edits peak windows.  The plugin uses the activity rhythm of
the three sites instead:

* workdays: 09:00–12:00 and 14:00–18:00 are peak
* non-workdays (Saturday/Sunday): open all day, no peak restriction
* everything else is valley

Two access modes sit on top of that:

* ``valley_only`` — the bot may only be invoked outside workday peak periods
* ``all_day`` — always available, but weekday peak activity is slowed down by
  ``peak_frequency_percent``
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Callable

from .util import format_minutes


class Phase(str, Enum):
    VALLEY = "valley"
    PEAK = "peak"
    OPEN = "open"

    @property
    def label(self) -> str:
        return {"valley": "谷时", "peak": "峰时", "open": "全天开放"}[self.value]


# Start/end are minutes since midnight.  End is exclusive.
WEEKDAY_PEAK = ((9 * 60, 12 * 60), (14 * 60, 18 * 60))
WEEKEND_PEAK: tuple[tuple[int, int], ...] = ()

ACCESS_MODES = {
    "valley_only": "仅谷时可用",
    "all_day": "全天开放",
}


def _in_windows(minute: int, windows: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= minute < end for start, end in windows)


def is_open_day(dt: datetime) -> bool:
    """Non-workdays are open all day (no peak restriction)."""
    return dt.weekday() >= 5


def peak_windows_for(dt: datetime) -> tuple[tuple[int, int], ...]:
    return WEEKEND_PEAK if is_open_day(dt) else WEEKDAY_PEAK


def format_windows(windows: tuple[tuple[int, int], ...]) -> list[str]:
    return [f"{format_minutes(start)}-{format_minutes(end)}" for start, end in windows]


class AutoSchedule:
    def __init__(
        self,
        access_mode: str = "all_day",
        peak_frequency_percent: int = 40,
        is_off_day: Callable[[datetime], bool] | None = None,
    ) -> None:
        self.access_mode = access_mode if access_mode in ACCESS_MODES else "all_day"
        self.peak_frequency_percent = max(5, min(100, int(peak_frequency_percent)))
        self._is_off_day = is_off_day or (lambda value: value.weekday() >= 5)

    @property
    def valley_only(self) -> bool:
        return self.access_mode == "valley_only"

    def is_open_day(self, dt: datetime) -> bool:
        """Holiday calendar and weekends are open all day."""
        return bool(self._is_off_day(dt))

    def phase(self, dt: datetime) -> Phase:
        if self.is_open_day(dt):
            return Phase.OPEN
        minute = dt.hour * 60 + dt.minute
        return Phase.PEAK if _in_windows(minute, WEEKDAY_PEAK) else Phase.VALLEY

    def can_act(self, dt: datetime) -> bool:
        """Whether an autonomous action may run right now."""
        return not self.valley_only or self.phase(dt) is not Phase.PEAK

    def can_call(self, dt: datetime) -> bool:
        """Whether a chat/tool invocation is allowed right now."""
        return self.can_act(dt)

    def interval_multiplier(self, dt: datetime) -> float:
        """Slow autonomous activity during weekday peak in ``all_day`` mode."""
        if self.phase(dt) is not Phase.PEAK:
            return 1.0
        if self.valley_only:
            # Activity is blocked in this mode; keep normal intervals so the
            # first valley action after peak is not scheduled too early.
            return 1.0
        # 40% frequency -> 2.5x interval
        return 100.0 / max(5, self.peak_frequency_percent)

    def describe(self, dt: datetime | None = None) -> dict:
        dt = dt or datetime.now()
        windows = () if self.is_open_day(dt) else WEEKDAY_PEAK
        return {
            "mode": self.access_mode,
            "mode_label": ACCESS_MODES[self.access_mode],
            "peak_frequency_percent": self.peak_frequency_percent,
            "peak_windows": format_windows(windows),
            "rule": "工作日 09:00-12:00、14:00-18:00 为峰时；周末和法定节假日全天开放",
            "phase": self.phase(dt).value,
            "phase_label": self.phase(dt).label,
            "available_now": self.can_act(dt),
            "is_open_day": self.is_open_day(dt),
        }
