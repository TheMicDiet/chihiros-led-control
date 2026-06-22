"""Manual dosing helpers and persisted daily counters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .vendor.chihiros_led_control.models import DOSING_PUMP

STORAGE_KEY = f"{DOMAIN}_dosing_daily_totals"
STORAGE_VERSION = 1
CONF_PUMP_COUNT = "pump_count"
PUMP_COUNT = 4
PUMP_COUNT_OPTIONS = (2, 4)
SIGNAL_DOSING_TOTALS_UPDATED = f"{DOMAIN}_dosing_totals_updated"


@dataclass
class DosingDailyTotals:
    """Persisted daily dosing totals for one device."""

    hass: HomeAssistant
    address: str
    pump_count: int = PUMP_COUNT
    _store: Store[dict[str, Any]] = field(init=False)
    _date: str = field(init=False)
    _totals: list[float] = field(init=False)
    _unsub_midnight_reset: Any = None

    def __post_init__(self) -> None:
        """Initialize storage metadata."""
        self._store = Store(self.hass, STORAGE_VERSION, f"{STORAGE_KEY}_{self.address.lower().replace(':', '_')}")
        self.pump_count = normalize_pump_count(self.pump_count)
        self._date = self._today()
        self._totals = [0.0] * self.pump_count

    @property
    def address_signal(self) -> str:
        """Return the dispatcher signal for this device's dosing totals."""
        return f"{SIGNAL_DOSING_TOTALS_UPDATED}_{self.address.lower()}"

    async def async_load(self) -> None:
        """Load today's totals from Home Assistant storage."""
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            stored_date = stored.get("date")
            stored_totals = stored.get("totals_ml")
            if stored_date == self._today() and isinstance(stored_totals, list):
                self._date = stored_date
                self._totals = [
                    _coerce_total(stored_totals[index] if index < len(stored_totals) else 0.0)
                    for index in range(self.pump_count)
                ]
            else:
                await self.async_reset()
        self._schedule_midnight_reset()

    def total_ml(self, pump_idx: int) -> float:
        """Return today's total for a zero-based pump index."""
        self._ensure_today_sync()
        self._validate_pump_idx(pump_idx)
        return self._totals[pump_idx]

    async def async_add_dose(self, pump_idx: int, volume_ml: float) -> None:
        """Add a successful manual dose to today's local total."""
        self._ensure_today_sync()
        self._validate_pump_idx(pump_idx)
        self._totals[pump_idx] = round(self._totals[pump_idx] + volume_ml, 1)
        await self.async_save()
        async_dispatcher_send(self.hass, self.address_signal)

    async def async_reset(self) -> None:
        """Reset totals for the current day."""
        self._date = self._today()
        self._totals = [0.0] * self.pump_count
        await self.async_save()
        async_dispatcher_send(self.hass, self.address_signal)

    async def async_save(self) -> None:
        """Persist totals to Home Assistant storage."""
        await self._store.async_save({"date": self._date, "totals_ml": self._totals})

    def async_close(self) -> None:
        """Cancel scheduled callbacks."""
        if self._unsub_midnight_reset:
            self._unsub_midnight_reset()
            self._unsub_midnight_reset = None

    def _ensure_today_sync(self) -> None:
        today = self._today()
        if self._date != today:
            self._date = today
            self._totals = [0.0] * self.pump_count

    def _today(self) -> str:
        return dt_util.now().date().isoformat()

    def _schedule_midnight_reset(self) -> None:
        """Schedule the next local midnight reset while Home Assistant keeps running."""
        if self._unsub_midnight_reset:
            self._unsub_midnight_reset()
        next_midnight = dt_util.start_of_local_day(dt_util.now() + timedelta(days=1))
        self._unsub_midnight_reset = async_track_point_in_time(self.hass, self._async_midnight_reset, next_midnight)

    async def _async_midnight_reset(self, _now: Any) -> None:
        """Reset totals at local midnight and schedule the next reset."""
        await self.async_reset()
        self._schedule_midnight_reset()

    def _validate_pump_idx(self, pump_idx: int) -> None:
        if pump_idx < 0 or pump_idx >= self.pump_count:
            raise ValueError(f"Pump index must be between 0 and {self.pump_count - 1}")


def is_dosing_capable(device: object) -> bool:
    """Return whether a runtime client or model supports manual dosing."""
    return getattr(device, "model_name", getattr(device, "name", None)) == DOSING_PUMP.name


def normalize_pump_count(value: object) -> int:
    """Return a supported dosing pump count, defaulting to four pumps."""
    try:
        pump_count = int(value)
    except (TypeError, ValueError):
        return PUMP_COUNT
    if pump_count in PUMP_COUNT_OPTIONS:
        return pump_count
    return PUMP_COUNT


def _coerce_total(value: object) -> float:
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return 0.0
