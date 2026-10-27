"""China public-holiday calendar with a local cache.

The official ``holiday-cn`` dataset is published as one JSON file per year.
We cache it under the plugin data directory and fall back to Saturday/Sunday
when the network is unavailable.  A user may also add company holidays or
make-up workdays from the settings page.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import httpx

from .util import atomic_write_json, now_tz, read_json


HOLIDAY_URLS = (
    "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json",
    "https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json",
    "https://fastly.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json",
)
CACHE_TTL_SECONDS = 12 * 60 * 60


@dataclass
class HolidayCalendar:
    data_dir: Path
    enabled: bool = True
    extra_off: set[str] = field(default_factory=set)
    extra_work: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.cache_dir = self.data_dir / "holidays"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.bundle_dir = Path(__file__).resolve().parent / "holidays_data"
        self.days: dict[str, tuple[bool, str]] = {}
        self.loaded_years: set[int] = set()
        self._lock = asyncio.Lock()
        for year in (date.today().year - 1, date.today().year, date.today().year + 1):
            self._load_cache(year)

    # ----------------------------------------------------------------- cache
    def _cache_path(self, year: int) -> Path:
        return self.cache_dir / f"{year}.json"

    def _load_file(self, path: Path, year: int) -> bool:
        data = read_json(path, {})
        if not isinstance(data, dict) or data.get("year") != year:
            return False
        count = 0
        for item in data.get("days") or []:
            if not isinstance(item, dict):
                continue
            day = str(item.get("date") or "")
            if not day:
                continue
            self.days[day] = (bool(item.get("isOffDay")), str(item.get("name") or ""))
            count += 1
        if count:
            self.loaded_years.add(year)
        return count > 0

    def _load_cache(self, year: int) -> bool:
        if self._load_file(self._cache_path(year), year):
            return True
        # Ship 2026 data with the plugin so holiday recognition works before
        # the first successful network refresh.
        return self._load_file(self.bundle_dir / f"{year}.json", year)

    def _cache_fresh(self, year: int) -> bool:
        path = self._cache_path(year)
        try:
            return (datetime.now().timestamp() - path.stat().st_mtime) < CACHE_TTL_SECONDS
        except OSError:
            return False

    async def refresh(self, years: list[int] | None = None) -> dict[int, bool]:
        async with self._lock:
            return await self._refresh_locked(years)

    async def _refresh_locked(self, years: list[int] | None = None) -> dict[int, bool]:
        today = date.today()
        years = years or [today.year - 1, today.year, today.year + 1]
        result: dict[int, bool] = {}
        for year in years:
            if self._cache_fresh(year):
                result[year] = self._load_cache(year)
                continue
            try:
                data = None
                for template in HOLIDAY_URLS:
                    try:
                        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                            response = await client.get(template.format(year=year))
                        if response.status_code != 200:
                            continue
                        candidate = response.json()
                        if isinstance(candidate, dict) and candidate.get("year") == year:
                            data = candidate
                            break
                    except Exception:  # noqa: BLE001
                        continue
                if not data:
                    result[year] = False
                    continue
                atomic_write_json(self._cache_path(year), data)
                result[year] = self._load_cache(year)
            except Exception:  # noqa: BLE001 - network is optional
                result[year] = False
            await asyncio.sleep(0.15)
        return result

    # ---------------------------------------------------------------- lookup
    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)

    def set_custom(self, off_dates: list[str] | None = None, work_dates: list[str] | None = None) -> None:
        self.extra_off = {str(item).strip() for item in (off_dates or []) if str(item).strip()}
        self.extra_work = {str(item).strip() for item in (work_dates or []) if str(item).strip()}

    def is_off_day(self, value: date | datetime) -> bool:
        day = value.date() if isinstance(value, datetime) else value
        iso = day.isoformat()
        if iso in self.extra_work:
            return False
        if iso in self.extra_off:
            return True
        if self.enabled and iso in self.days:
            return self.days[iso][0]
        return day.weekday() >= 5

    def name_for(self, value: date | datetime) -> str:
        day = value.date() if isinstance(value, datetime) else value
        item = self.days.get(day.isoformat())
        return item[1] if item and item[0] else ""

    def status(self, value: date | datetime) -> dict:
        day = value.date() if isinstance(value, datetime) else value
        iso = day.isoformat()
        off = self.is_off_day(day)
        source = "weekend"
        if iso in self.extra_work:
            source = "custom_workday"
        elif iso in self.extra_off:
            source = "custom_holiday"
        elif self.enabled and iso in self.days:
            source = "official_calendar"
        return {
            "date": iso,
            "is_off_day": off,
            "name": self.name_for(day),
            "source": source,
            "calendar_enabled": self.enabled,
            "known_years": sorted(self.loaded_years),
        }

    async def refresh_for_now(self) -> dict[int, bool]:
        now = now_tz().date()
        return await self.refresh([now.year - 1, now.year, now.year + 1])
