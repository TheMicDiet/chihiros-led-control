"""Manual dosing helpers and persisted daily counters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .vendor.chihiros_led_control.models import DOSING_PUMP

STORAGE_KEY = f"{DOMAIN}_dosing_daily_totals"
STORAGE_VERSION = 1
PROGRAMMING_STORAGE_KEY = f"{DOMAIN}_dosing_programming"
CALIBRATION_STORAGE_KEY = f"{DOMAIN}_dosing_calibration"
SIGNAL_DOSING_TOTALS_UPDATED = f"{DOMAIN}_dosing_totals_updated"
SIGNAL_DOSING_CALIBRATION_UPDATED = f"{DOMAIN}_dosing_calibration_updated"
CONF_PUMP_COUNT = "pump_count"
PUMP_COUNT = 4
PUMP_COUNT_OPTIONS = (2, 4, 8)
# The stirrer speaks the dosing-pump protocol with up to 8 channels
# (DOSING_CONTROL.md §2/§6.1); the config flow lets owners of smaller units
# hide the channels they do not use.
CONF_STIRRER_CHANNEL_COUNT = "stirrer_channel_count"
STIRRER_CHANNEL_MAX = 8
STIRRER_CHANNEL_COUNT_OPTIONS = (2, 4, 8)


@dataclass
class DosingDailyTotals:
    """Persisted daily dosing totals for one device."""

    hass: HomeAssistant
    address: str
    pump_count: int = PUMP_COUNT
    _store: Store[dict[str, Any]] = field(init=False)
    _date: str = field(init=False)
    _totals: list[float] = field(init=False)
    _lifetime_ml: list[float] = field(init=False)
    _lifetime_cycles: list[int] = field(init=False)
    _unsub_midnight_reset: Any = None

    def __post_init__(self) -> None:
        """Initialize storage metadata."""
        self._store = Store(self.hass, STORAGE_VERSION, f"{STORAGE_KEY}_{self.address.lower().replace(':', '_')}")
        self.pump_count = normalize_pump_count(self.pump_count)
        self._date = self._today()
        self._totals = [0.0] * self.pump_count
        self._lifetime_ml = [0.0] * self.pump_count
        self._lifetime_cycles = [0] * self.pump_count

    @property
    def address_signal(self) -> str:
        """Return the dispatcher signal for this device's dosing totals."""
        return f"{SIGNAL_DOSING_TOTALS_UPDATED}_{self.address.lower()}"

    async def async_load(self) -> None:
        """Load today's totals from Home Assistant storage."""
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self._lifetime_ml = _coerce_total_list(stored.get("lifetime_ml"), self.pump_count)
            self._lifetime_cycles = _coerce_cycles_list(stored.get("lifetime_cycles"), self.pump_count)
            stored_date = stored.get("date")
            stored_totals = stored.get("totals_ml")
            if stored_date == self._today() and isinstance(stored_totals, list):
                self._date = stored_date
                self._totals = _coerce_total_list(stored_totals, self.pump_count)
            else:
                await self.async_reset()
        self._schedule_midnight_reset()

    def total_ml(self, pump_idx: int) -> float:
        """Return today's total for a zero-based pump index."""
        self._ensure_today_sync()
        self._validate_pump_idx(pump_idx)
        return self._totals[pump_idx]

    def lifetime_ml(self, pump_idx: int) -> float:
        """Return the lifetime dosed volume for a zero-based pump index."""
        self._validate_pump_idx(pump_idx)
        return self._lifetime_ml[pump_idx]

    def lifetime_cycles(self, pump_idx: int) -> int:
        """Return the lifetime dose count for a zero-based pump index."""
        self._validate_pump_idx(pump_idx)
        return self._lifetime_cycles[pump_idx]

    async def async_add_dose(self, pump_idx: int, volume_ml: float) -> None:
        """Add a successful manual dose to today's local total."""
        self._ensure_today_sync()
        self._validate_pump_idx(pump_idx)
        self._totals[pump_idx] = round(self._totals[pump_idx] + volume_ml, 1)
        self._lifetime_ml[pump_idx] = round(self._lifetime_ml[pump_idx] + volume_ml, 1)
        self._lifetime_cycles[pump_idx] += 1
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
        await self._store.async_save(
            {
                "date": self._date,
                "totals_ml": self._totals,
                "lifetime_ml": self._lifetime_ml,
                "lifetime_cycles": self._lifetime_cycles,
            }
        )

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


@dataclass
class DosingCalibrationTracker:
    """Persisted record of one pump channel's last calibration.

    The wizard writes a record after a channel's measured volume has been
    submitted; the ``last_calibration`` sensors read from here. Records keep
    the test run duration and the measured volume for reference.
    """

    hass: HomeAssistant
    address: str
    _store: Store[dict[str, Any]] = field(init=False)
    _channels: dict[int, dict[str, Any]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        """Create the per-address store."""
        self._store = Store(
            self.hass,
            STORAGE_VERSION,
            f"{CALIBRATION_STORAGE_KEY}_{self.address.lower().replace(':', '_')}",
        )

    async def async_load(self) -> None:
        """Load recorded calibrations from Home Assistant storage."""
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self._channels = {int(channel): record for channel, record in stored.get("channels", {}).items()}

    @property
    def address_signal(self) -> str:
        """Return the dispatcher signal for this device's calibration records."""
        return f"{SIGNAL_DOSING_CALIBRATION_UPDATED}_{self.address.lower()}"

    def record(self, channel: int) -> dict[str, Any] | None:
        """Return one channel's calibration record, or None."""
        stored = self._channels.get(channel)
        return dict(stored) if stored else None

    def calibrated_at(self, channel: int) -> datetime | None:
        """Return one channel's last calibration timestamp, if any."""
        stored = self._channels.get(channel)
        if not stored or not stored.get("calibrated"):
            return None
        try:
            return datetime.fromisoformat(str(stored["calibrated"]))
        except ValueError:
            return None

    async def async_record(self, channel: int, *, seconds: int | None, volume_ml: float | None) -> None:
        """Store one channel's calibration result and persist it."""
        self._channels[channel] = {
            "calibrated": dt_util.now().isoformat(),
            "seconds": seconds,
            "volume_ml": volume_ml,
        }
        await self._async_save()
        async_dispatcher_send(self.hass, self.address_signal)

    async def _async_save(self) -> None:
        """Persist the per-channel calibration records."""
        await self._store.async_save({"channels": {str(channel): record for channel, record in self._channels.items()}})


def is_dosing_capable(device: object) -> bool:
    """Return whether a runtime client or model supports manual dosing."""
    return getattr(device, "model_name", getattr(device, "name", None)) == DOSING_PUMP.name


@dataclass
class DosingProgrammingTracker:
    """Persisted record of one pump's channel programming.

    Home Assistant cannot read schedules back from the device, so every
    programming write made through Home Assistant is recorded here. The
    master/slave mirror uses these records to replay a pump's channels onto a
    linked stirrer (the app's ``startAsSlave``, DOSING_CONTROL.md §6.4).
    Records are partial per channel and merged on update, so an
    active-flag-only write keeps the stored schedule.

    ``device_settings`` holds device-level flags (currently ``dose_delay``)
    that the app mirrors onto the stirrer after the channel loop (§6.4).
    """

    hass: HomeAssistant
    address: str
    _store: Store[dict[str, Any]] = field(init=False)
    _channels: dict[int, dict[str, Any]] = field(init=False, default_factory=dict)
    _device: dict[str, Any] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        """Create the per-address store."""
        self._store = Store(
            self.hass,
            STORAGE_VERSION,
            f"{PROGRAMMING_STORAGE_KEY}_{self.address.lower().replace(':', '_')}",
        )

    async def async_load(self) -> None:
        """Load recorded channel programming from Home Assistant storage."""
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self._channels = {int(channel): record for channel, record in stored.get("channels", {}).items()}
            device = stored.get("device")
            if isinstance(device, dict):
                self._device = device

    @property
    def channels(self) -> dict[int, dict[str, Any]]:
        """Return a copy of the recorded per-channel programming."""
        return {channel: dict(record) for channel, record in self._channels.items()}

    @property
    def device_settings(self) -> dict[str, Any]:
        """Return a copy of the recorded device-level settings."""
        return dict(self._device)

    async def async_record(self, channel: int, setup: dict[str, Any], *, stamp_programmed: bool = True) -> None:
        """Merge one channel's programming write into the record and persist it.

        ``stamp_programmed`` records the date of the write; schedule writes
        (which send the device's ``dosingSet`` frame) set it, so the service
        can derive the "first setting of the day" flag. State-only writes
        (``set_channel_active``) keep any earlier stamp.
        """
        record = {**self._channels.get(channel, {}), **setup}
        if stamp_programmed:
            record["last_programmed"] = self._today()
        self._channels[channel] = record
        await self._async_save()

    def channel_programmed_today(self, channel: int) -> bool:
        """Return whether the channel's schedule was written through HA today."""
        record = self._channels.get(channel)
        return bool(record) and record.get("last_programmed") == self._today()

    def _today(self) -> str:
        """Return the local date string used for programming stamps."""
        return dt_util.now().date().isoformat()

    async def async_record_device(self, settings: dict[str, Any]) -> None:
        """Merge device-level settings into the record and persist them."""
        self._device = {**self._device, **settings}
        await self._async_save()

    async def async_clear_channel(self, channel: int) -> None:
        """Drop one channel's record (app's ``resetChannel``) and persist."""
        self._channels.pop(channel, None)
        await self._async_save()

    async def _async_save(self) -> None:
        """Persist channels and device settings."""
        await self._store.async_save(
            {
                "channels": {str(channel): record for channel, record in self._channels.items()},
                "device": self._device,
            }
        )


def derive_first_setting(tracker: DosingProgrammingTracker | None, channel: int, explicit: bool | None) -> bool:
    """Return the device's ``first_setting`` flag for a channel write.

    The device resets its daily counters on the first ``dosingSet`` of the day
    for a channel, so the flag is True unless this channel was already
    programmed through Home Assistant today. An explicit service value wins;
    without a programming record the flag defaults to True.
    """
    if explicit is not None:
        return explicit
    if tracker is None:
        return True
    return not tracker.channel_programmed_today(channel)


def normalize_pump_count(value: object) -> int:
    """Return a supported dosing pump count, defaulting to four pumps."""
    try:
        pump_count = int(value)
    except (TypeError, ValueError):
        return PUMP_COUNT
    if pump_count in PUMP_COUNT_OPTIONS:
        return pump_count
    return PUMP_COUNT


def normalize_stirrer_channel_count(value: object) -> int:
    """Return a supported stirrer channel count, defaulting to all channels."""
    try:
        count = int(value)
    except (TypeError, ValueError):
        return STIRRER_CHANNEL_MAX
    if count in STIRRER_CHANNEL_COUNT_OPTIONS:
        return count
    return STIRRER_CHANNEL_MAX


def entry_pump_count(entry: ConfigEntry) -> int:
    """Return a config entry's pump count, preferring options over data."""
    return normalize_pump_count(entry.options.get(CONF_PUMP_COUNT, entry.data.get(CONF_PUMP_COUNT)))


def entry_stirrer_channel_count(entry: ConfigEntry) -> int:
    """Return a config entry's stirrer channel count, preferring options over data."""
    value = entry.options.get(CONF_STIRRER_CHANNEL_COUNT, entry.data.get(CONF_STIRRER_CHANNEL_COUNT))
    return normalize_stirrer_channel_count(value)


def _coerce_total(value: object) -> float:
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return 0.0


def _coerce_total_list(values: object, pump_count: int) -> list[float]:
    """Coerce a stored list of per-pump totals into a fixed-length float list."""
    if not isinstance(values, list):
        return [0.0] * pump_count
    return [_coerce_total(values[index]) if index < len(values) else 0.0 for index in range(pump_count)]


def _coerce_cycles_list(values: object, pump_count: int) -> list[int]:
    """Coerce a stored list of per-pump cycle counts into a fixed-length int list."""
    if not isinstance(values, list):
        return [0] * pump_count
    coerced: list[int] = []
    for index in range(pump_count):
        if index >= len(values):
            coerced.append(0)
            continue
        try:
            coerced.append(int(round(float(values[index]))))
        except (TypeError, ValueError):
            coerced.append(0)
    return coerced
