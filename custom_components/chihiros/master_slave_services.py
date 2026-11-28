"""Master/slave support: pump programming services and stirrer mirroring.

Home Assistant has no BLE broadcast: the vendor app mirrors a linked stirrer
by broadcasting the pump's ``startWork`` frames to every connected device
(DOSING_CONTROL.md §5/§6.4). This module reproduces that behaviour explicitly:

- ``chihiros.set_dosing_schedule`` / ``chihiros.set_channel_active`` program a
  dosing pump channel and record the write. If a stirrer entry is linked to
  the pump (master), the same write is replayed to the stirrer immediately —
  the equivalent of the app's broadcast.
- ``chihiros.set_stirrer_master`` persists the link on the stirrer's config
  entry (``master_address``; the app's persisted-only bookkeeping) and can
  replay the pump's full recorded programming (``startAsSlave``).
- ``chihiros.mirror_stirrer`` replays the pump's recorded programming onto the
  linked stirrer on demand.
"""

from __future__ import annotations

import datetime
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_MASTER_ADDRESS, DOMAIN
from .dosing import derive_first_setting, deserialize_points, is_legacy_dosing_pump, serialize_points
from .models import DosingChihirosData, StirrerChihirosData
from .service_utils import (
    ATTR_ADDRESS,
    ATTR_DEVICE_ID,
    ATTR_ENTRY_ID,
    ATTR_WEEKDAYS,
    DEVICE_SELECTOR_SCHEMA,
    WEEKDAY_VALUES,
    frequency_from_service_data,
    parse_start_minutes,
    resolve_service_device,
)
from .stirrer import set_stirrer_pre_run_entities_enabled, stirrer_client
from .stirrer_services import (
    ATTR_ACTIVE,
    ATTR_CHANNEL,
    ATTR_FIRST_SETTING,
    ATTR_FREQUENCY,
)
from .vendor.chihiros_led_control.protocol.dosing import (
    DOSE_VOLUME_MAX_ML,
    DosingMode,
    DosingWorkPoint,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_DOSING_SCHEDULE = "set_dosing_schedule"
SERVICE_SET_CHANNEL_ACTIVE = "set_channel_active"
SERVICE_SET_DOSE_DELAY = "set_dose_delay"
SERVICE_RESET_DOSING_CHANNEL = "reset_dosing_channel"
SERVICE_SET_STIRRER_MASTER = "set_stirrer_master"
SERVICE_MIRROR_STIRRER = "mirror_stirrer"

ATTR_ENABLED = "enabled"
ATTR_MASTER_DEVICE_ID = "master_device_id"

ATTR_MODE = "mode"
ATTR_POINTS = "points"
ATTR_ENABLE = "enable"
ATTR_COMPENSATE = "compensate"
# Kept as an ``ATTR_`` name for the service schema/API; the value is shared with
# the options flow, which stores the same link on the stirrer config entry.
ATTR_MASTER_ADDRESS = CONF_MASTER_ADDRESS
ATTR_MASTER_ENTRY_ID = "master_entry_id"
ATTR_MIRROR = "mirror"
ATTR_DELAY = "delay"
ATTR_DAILY_ML = "daily_ml"

MINUTES_PER_DAY = 24 * 60
DOSE_MODES = ("single", "auto", "free", "timer")
FREQUENCY_SUNDAY_FIRST = "frequency_sunday_first"

DOSE_POINT_SCHEMA = vol.Schema(
    {
        vol.Required("start"): vol.Any(str, datetime.time),
        vol.Optional("ml"): vol.All(vol.Coerce(float), vol.Range(min=0, max=DOSE_VOLUME_MAX_ML)),
        vol.Optional("end"): vol.Any(str, datetime.time),
        vol.Optional("count"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
    }
)

SET_DOSING_SCHEDULE_SCHEMA = vol.Schema(
    {
        **DEVICE_SELECTOR_SCHEMA,
        vol.Required(ATTR_CHANNEL): vol.All(vol.Coerce(int), vol.Range(min=1, max=8)),
        vol.Required(ATTR_MODE): vol.All(vol.Lower, vol.In(DOSE_MODES)),
        vol.Required(ATTR_POINTS): vol.All([dict], vol.Length(min=1), [DOSE_POINT_SCHEMA]),
        vol.Exclusive(ATTR_WEEKDAYS, "repeat"): vol.All(list, [vol.In(WEEKDAY_VALUES)]),
        vol.Exclusive(ATTR_FREQUENCY, "repeat"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
        vol.Optional(ATTR_ACTIVE, default=True): vol.Boolean(),
        # Derived from the programming record when omitted (first write of the
        # day for the channel); exposed for advanced/backwards-compatible use.
        vol.Optional(ATTR_FIRST_SETTING): vol.Boolean(),
        vol.Optional(ATTR_DAILY_ML): vol.All(vol.Coerce(float), vol.Range(min=0, max=DOSE_VOLUME_MAX_ML)),
    }
)

SET_CHANNEL_ACTIVE_SCHEMA = vol.Schema(
    {
        **DEVICE_SELECTOR_SCHEMA,
        vol.Required(ATTR_CHANNEL): vol.All(vol.Coerce(int), vol.Range(min=1, max=8)),
        vol.Optional(ATTR_ENABLE, default=True): vol.Boolean(),
        vol.Optional(ATTR_COMPENSATE, default=False): vol.Boolean(),
    }
)

SET_DOSE_DELAY_SCHEMA = vol.Schema(
    {
        **DEVICE_SELECTOR_SCHEMA,
        vol.Optional(ATTR_ENABLED, default=True): vol.Boolean(),
    }
)

RESET_DOSING_CHANNEL_SCHEMA = vol.Schema(
    {
        **DEVICE_SELECTOR_SCHEMA,
        vol.Required(ATTR_CHANNEL): vol.All(vol.Coerce(int), vol.Range(min=1, max=8)),
    }
)

MASTER_SELECTOR_SCHEMA = {
    vol.Exclusive(ATTR_MASTER_DEVICE_ID, "master"): vol.All(str, vol.Length(min=1)),
    vol.Exclusive(ATTR_MASTER_ENTRY_ID, "master"): vol.All(str, vol.Length(min=1)),
    vol.Exclusive(ATTR_MASTER_ADDRESS, "master"): vol.All(str, vol.Length(min=1)),
}

SET_STIRRER_MASTER_SCHEMA = vol.Schema(
    {
        **DEVICE_SELECTOR_SCHEMA,
        **MASTER_SELECTOR_SCHEMA,
        vol.Optional(ATTR_MIRROR, default=True): vol.Boolean(),
        # None = replay the pump's recorded dose-delay flag (like mirror_stirrer);
        # an explicit value overrides it.
        vol.Optional(ATTR_DELAY): vol.Boolean(),
    }
)

MIRROR_STIRRER_SCHEMA = vol.Schema(
    {
        **DEVICE_SELECTOR_SCHEMA,
        # None = replay the recorded dose_delay; an explicit value overrides.
        vol.Optional(ATTR_DELAY): vol.Boolean(),
    }
)


def build_work_points(mode: str, points: list[dict[str, Any]]) -> list[DosingWorkPoint]:
    """Convert service point dicts into timer-mode work points for ``mode``."""
    if mode in ("single", "auto", "timer"):
        return [_volume_point(point) for point in points]
    if mode == "free":
        return [_free_point(point) for point in points]
    raise HomeAssistantError(f"Unknown dosing mode {mode!r}")


def _parse_point_time(point: dict[str, Any], key: str) -> int:
    """Parse one point time, converting validation errors to service errors."""
    try:
        return parse_start_minutes(point[key])
    except vol.Invalid as ex:
        raise HomeAssistantError(f"Invalid {key} time {point.get(key)!r}: {ex}") from ex


def _volume_point(point: dict[str, Any]) -> DosingWorkPoint:
    """Build one single/auto/timer point (``HH:MM`` + volume in mL)."""
    start = _parse_point_time(point, "start")
    if "ml" not in point:
        raise HomeAssistantError(f"Point {point.get('start')!r} needs an 'ml' volume in single/auto/timer mode")
    return DosingWorkPoint(start // 60, start % 60, volume_ml=float(point["ml"]))


def _free_point(point: dict[str, Any]) -> DosingWorkPoint:
    """Build one free-mode point (window + dose count, no volume)."""
    start = _parse_point_time(point, "start")
    if "end" not in point:
        raise HomeAssistantError(f"Point {point.get('start')!r} needs an 'end' time in free mode")
    end = _parse_point_time(point, "end")
    duration = end - start
    if duration <= 0:
        duration += MINUTES_PER_DAY  # windows may wrap past midnight
    count = int(point.get("count", 2))  # 2 = the app's DosingWorkPoint default
    return DosingWorkPoint(start // 60, start % 60, duration_minutes=duration, number=count)


def _config_entry_for_address(hass: HomeAssistant, address: str) -> ConfigEntry | None:
    """Find the config entry whose device advertises ``address``."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if str(entry.data.get(CONF_ADDRESS, "")).upper() == address.upper():
            return entry
    return None


def _linked_stirrers(hass: HomeAssistant, master_address: str) -> list[StirrerChihirosData]:
    """Return loaded stirrer entries whose persisted master is ``master_address``."""
    slaves: list[StirrerChihirosData] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if str(entry.data.get(ATTR_MASTER_ADDRESS, "")).upper() != master_address.upper():
            continue
        data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if isinstance(data, StirrerChihirosData):
            slaves.append(data)
    return slaves


def _resolve_master(hass: HomeAssistant, call_data: dict[str, Any]) -> DosingChihirosData | None:
    """Resolve the master pump from the service data, or None for unlink."""
    if call_data.get(ATTR_MASTER_DEVICE_ID):
        selector = {ATTR_DEVICE_ID: call_data[ATTR_MASTER_DEVICE_ID]}
    elif call_data.get(ATTR_MASTER_ENTRY_ID):
        selector = {ATTR_ENTRY_ID: call_data[ATTR_MASTER_ENTRY_ID]}
    elif call_data.get(ATTR_MASTER_ADDRESS):
        selector = {ATTR_ADDRESS: call_data[ATTR_MASTER_ADDRESS]}
    else:
        return None
    master = resolve_service_device(hass, selector)
    if not isinstance(master, DosingChihirosData):
        raise HomeAssistantError(f"{master.device.name} is not a dosing pump")
    return master


def _ensure_programmable(data: object) -> None:
    """Raise unless the target is a configured dosing-pump data record."""
    if isinstance(data, DosingChihirosData):
        return
    name = getattr(getattr(data, "device", None), "name", data)
    raise HomeAssistantError(f"{name} is not a dosing pump")


def _validate_pump_channel(data: DosingChihirosData, channel: int) -> None:
    """Reject channels the configured pump does not expose."""
    if channel >= data.dosing_totals.pump_count:
        raise HomeAssistantError(f"{data.device.name} has {data.dosing_totals.pump_count} pump channels configured")


def _recorded_frequency(setup: dict[str, Any], *, sunday_first: bool) -> int:
    """Translate weekday bits between legacy pump and ISO order, preserving bit 7."""
    frequency = setup.get("frequency", 127)
    # Preserve the actual run days under the old ISO clock, even for an old raw mask.
    if setup.get(FREQUENCY_SUNDAY_FIRST, False) == sunday_first:
        return frequency
    if sunday_first:
        return (frequency & 0x80) | ((frequency & 0x7E) >> 1) | ((frequency & 0x01) << 6)
    return (frequency & 0x80) | ((frequency & 0x3F) << 1) | ((frequency & 0x40) >> 6)


def _recorded_schedule(setup: dict[str, Any]) -> tuple[DosingMode | None, list[DosingWorkPoint]]:
    if not setup.get("mode"):
        return None, []
    return DosingMode[setup["mode"].upper()], deserialize_points(setup.get("points", []))


async def _apply_channel_setup(
    stirrer_device: object,
    channel: int,
    setup: dict[str, Any],
    *,
    legacy_pump: bool,
    start_as_slave: bool = False,
) -> None:
    """Mirror channel programming, without installing slots for an inactive legacy pump.

    A full replay (the app's ``startAsSlave``) includes daily settings even
    if absent from the record, in which case the daily volume defaults to 0.
    """
    client = stirrer_client(stirrer_device)
    active = setup.get("active", False)
    try:
        mode, points = _recorded_schedule(setup) if active or not legacy_pump else (None, [])
        await client.program_channel(
            channel,
            active=active,
            compensate=setup.get("compensate", False),
            dose_per_day_ml=setup.get("dose_per_day_ml", 0.0 if start_as_slave else None),
            frequency=_recorded_frequency(setup, sunday_first=False),
            is_first_setting=start_as_slave or setup.get("first_setting", True),
            mode=mode,
            points=points,
        )
    except (KeyError, ValueError) as ex:
        raise HomeAssistantError(f"Corrupt programming record for channel {channel + 1}: {ex}") from ex


async def _replay_recorded_channels(
    master_data: DosingChihirosData,
    stirrer_data: StirrerChihirosData,
    channel_count: int | None = None,
) -> list[int]:
    """Replay recorded pump channels, returning skipped 1-based channels."""
    tracker = master_data.dosing_programming
    legacy_pump = is_legacy_dosing_pump(master_data.device)
    configured_channels = len(stirrer_data.stirrer_states) if channel_count is None else channel_count
    skipped: list[int] = []
    for channel in sorted(tracker.channels):
        if configured_channels and channel >= configured_channels:
            skipped.append(channel + 1)
            continue
        async with tracker.channel_lock(channel):
            setup = tracker.channels.get(channel)
            if setup is not None:
                await _apply_channel_setup(
                    stirrer_data.device, channel, setup, legacy_pump=legacy_pump, start_as_slave=True
                )
    return skipped


async def async_mirror_pump_to_stirrer(
    master_data: DosingChihirosData,
    stirrer_data: StirrerChihirosData,
    channel_count: int | None = None,
    *,
    delay: bool | None = None,
) -> None:
    """Replay the pump's full recorded programming onto a stirrer (startAsSlave).

    ``delay=None`` (the default) replays the pump's *recorded* dose-delay
    flag (§6.4 mirrors ``master.dose_delay``); an explicit value overrides
    the record.
    """
    tracker = master_data.dosing_programming
    if not tracker.channels:
        raise HomeAssistantError(
            f"No channel programming recorded for {master_data.device.name}; "
            "program the pump with set_dosing_schedule first"
        )
    if delay is None:
        delay = bool(tracker.device_settings.get("dose_delay", False))
    stirrer = stirrer_client(stirrer_data.device)
    skipped = await _replay_recorded_channels(master_data, stirrer_data, channel_count=channel_count)
    await stirrer.set_dose_delay(delay)
    if skipped:
        _LOGGER.warning(
            "Skipped pump channels %s when mirroring to %s: only %s stir channels are configured",
            ", ".join(str(channel) for channel in skipped),
            stirrer_data.device.name,
            len(stirrer_data.stirrer_states) if channel_count is None else channel_count,
        )


async def _mirror_to_linked_stirrers(
    hass: HomeAssistant, data: DosingChihirosData, channel: int, setup: dict[str, Any]
) -> None:
    """Replay one channel write onto every stirrer linked to this pump.

    All slaves are attempted even if one fails; the combined errors are
    raised afterwards so a single unavailable stirrer does not leave the
    others silently un-mirrored.
    """
    failures: list[str] = []
    legacy_pump = is_legacy_dosing_pump(data.device)
    for slave in _linked_stirrers(hass, data.device.address):
        configured_channels = len(slave.stirrer_states)
        if configured_channels and channel >= configured_channels:
            _LOGGER.debug(
                "Skipping live mirror of channel %s to %s: only %s stir channels are configured",
                channel + 1,
                slave.device.name,
                configured_channels,
            )
            continue
        try:
            await _apply_channel_setup(slave.device, channel, setup, legacy_pump=legacy_pump)
        except Exception as ex:  # noqa: BLE001 — collect every failure for the combined error
            failures.append(f"{slave.device.name}: {ex}")
    if failures:
        raise HomeAssistantError(f"Pump was programmed, but mirroring channel {channel} failed: " + "; ".join(failures))


async def async_broadcast_frame_to_linked_stirrers(
    hass: HomeAssistant, master_address: str, frame: bytes | bytearray, action: str
) -> None:
    """Deliver a pump frame verbatim to every linked stirrer (app broadcast).

    The vendor app sends one ``DataSendEvent``'s frames byte-for-byte to all
    connected devices when a slave is linked (DOSING_CONTROL.md §5) — used
    for manual doses, channel resets, and totals resets, not just
    programming. All slaves are attempted; combined failures raise.
    """
    failures: list[str] = []
    for slave in _linked_stirrers(hass, master_address):
        try:
            await stirrer_client(slave.device).send_frame(frame)
        except Exception as ex:  # noqa: BLE001 — collect every failure for the combined error
            failures.append(f"{slave.device.name}: {ex}")
    if failures:
        raise HomeAssistantError(
            f"Pump {action} succeeded, but broadcasting to linked stirrers failed: " + "; ".join(failures)
        )


async def _record_and_propagate(
    record: Callable[[], Awaitable[None]], propagate: Callable[[], Awaitable[None]], action: str
) -> None:
    """Finish both post-write steps and report failures without obscuring pump success."""
    failures: list[str] = []
    for label, operation in (("local storage", record), ("linked stirrers", propagate)):
        try:
            await operation()
        except Exception as ex:  # noqa: BLE001 — neither failure should skip the other post-write step
            failures.append(f"{label}: {ex}")
    if failures:
        raise HomeAssistantError(f"Pump {action} succeeded, but " + "; ".join(failures))


async def _record_and_mirror(
    hass: HomeAssistant, data: DosingChihirosData, channel: int, setup: dict[str, Any], *, stamp_programmed: bool
) -> None:
    """Persist accepted programming and mirror even if local persistence fails."""
    await _record_and_propagate(
        functools.partial(data.dosing_programming.async_record, channel, setup, stamp_programmed=stamp_programmed),
        functools.partial(_mirror_to_linked_stirrers, hass, data, channel, setup),
        "programming",
    )


async def _mirror_dose_delay(hass: HomeAssistant, address: str, enabled: bool) -> None:
    """Attempt the accepted delay setting on every linked stirrer."""
    failures: list[str] = []
    for slave in _linked_stirrers(hass, address):
        try:
            await stirrer_client(slave.device).set_dose_delay(enabled)
        except Exception as ex:  # noqa: BLE001 — attempt every slave before reporting errors
            failures.append(f"{slave.device.name}: {ex}")
    if failures:
        raise HomeAssistantError("; ".join(failures))


async def _async_set_dose_delay(hass: HomeAssistant, call: ServiceCall) -> None:
    """Set the pump's dose-delay flag, record it, and mirror it to linked stirrers."""
    data = resolve_service_device(hass, call.data)
    _ensure_programmable(data)
    enabled = bool(call.data[ATTR_ENABLED])
    async with data.dosing_programming.channel_lock(-1):
        await data.device.set_dose_delay(enabled)
        await _record_and_propagate(
            functools.partial(data.dosing_programming.async_record_device, {"dose_delay": enabled}),
            functools.partial(_mirror_dose_delay, hass, data.device.address, enabled),
            "dose delay",
        )


async def _async_reset_dosing_channel(hass: HomeAssistant, call: ServiceCall) -> None:
    """Reset one pump channel, drop its record, and broadcast the reset frame."""
    data = resolve_service_device(hass, call.data)
    _ensure_programmable(data)
    channel = int(call.data[ATTR_CHANNEL]) - 1
    _validate_pump_channel(data, channel)
    async with data.dosing_programming.channel_lock(channel):
        frame = await data.device.reset_channel(channel)
        await _record_and_propagate(
            functools.partial(data.dosing_programming.async_clear_channel, channel),
            functools.partial(
                async_broadcast_frame_to_linked_stirrers, hass, data.device.address, frame, "channel reset"
            ),
            "channel reset",
        )


def _pump_schedule_frequency(data: dict[str, Any], daily_ml: float | None, *, sunday_first: bool) -> int | None:
    """Only the daily-volume frame can set weekdays on the pump."""
    if daily_ml is not None:
        return frequency_from_service_data(data, sunday_first=sunday_first)
    if ATTR_WEEKDAYS in data or ATTR_FREQUENCY in data:
        raise HomeAssistantError("Setting weekdays or frequency requires daily_ml")
    return None


async def _resume_legacy_channel(
    hass: HomeAssistant, data: DosingChihirosData, channel: int, *, compensate: bool
) -> None:
    """Reinstall the saved schedule; a state-only write cannot enable a legacy pump."""
    tracker = data.dosing_programming
    setup = tracker.channels.get(channel, {})
    try:
        mode, points = _recorded_schedule(setup)
    except (KeyError, ValueError) as ex:
        raise HomeAssistantError(f"Corrupt programming record for channel {channel + 1}: {ex}") from ex
    if mode is None or not points:
        raise HomeAssistantError(f"No stored schedule for pump channel {channel + 1}; use set_dosing_schedule first")
    daily_ml = setup.get("dose_per_day_ml")
    frequency = _recorded_frequency(setup, sunday_first=True) if daily_ml is not None else None
    first_setting = derive_first_setting(tracker, channel, None)
    await data.device.program_channel(
        channel,
        active=True,
        compensate=compensate,
        dose_per_day_ml=daily_ml,
        frequency=frequency,
        is_first_setting=first_setting,
        mode=mode,
        points=points,
    )
    setup.update(active=True, compensate=compensate, first_setting=first_setting)
    if daily_ml is not None:
        setup.update({"frequency": frequency, FREQUENCY_SUNDAY_FIRST: True})
    await _record_and_mirror(hass, data, channel, setup, stamp_programmed=daily_ml is not None)


def async_register_pump_services(hass: HomeAssistant) -> None:
    """Register the dosing pump programming services."""

    async def async_set_dosing_schedule(call: ServiceCall) -> None:
        """Program one pump channel and mirror it to linked stirrers."""
        data = resolve_service_device(hass, call.data)
        _ensure_programmable(data)
        channel = int(call.data[ATTR_CHANNEL]) - 1
        _validate_pump_channel(data, channel)
        mode = call.data[ATTR_MODE]
        points = build_work_points(mode, call.data[ATTR_POINTS])
        daily_ml = call.data.get(ATTR_DAILY_ML)
        sunday_first = is_legacy_dosing_pump(data.device)
        frequency = _pump_schedule_frequency(call.data, daily_ml, sunday_first=sunday_first)
        async with data.dosing_programming.channel_lock(channel):
            first_setting = derive_first_setting(data.dosing_programming, channel, call.data.get(ATTR_FIRST_SETTING))
            # Structured programming retains its existing idempotent batch retry policy.
            # Failed or cancelled batches may leave partial hardware state, so only
            # successful writes are committed locally and propagated.
            await data.device.program_channel(
                channel,
                active=bool(call.data[ATTR_ACTIVE]),
                compensate=False,
                dose_per_day_ml=daily_ml,
                frequency=frequency,
                is_first_setting=first_setting,
                mode=DosingMode[mode.upper()],
                points=points,
            )
            setup: dict[str, Any] = {
                "active": bool(call.data[ATTR_ACTIVE]),
                "compensate": False,
                "mode": mode,
                "points": serialize_points(points),
                "first_setting": first_setting,
            }
            if daily_ml is not None:
                setup.update(
                    {"dose_per_day_ml": float(daily_ml), "frequency": frequency, FREQUENCY_SUNDAY_FIRST: sunday_first}
                )
            await _record_and_mirror(hass, data, channel, setup, stamp_programmed=daily_ml is not None)

    async def async_set_channel_active(call: ServiceCall) -> None:
        """Enable/disable one pump channel and mirror it to linked stirrers."""
        data = resolve_service_device(hass, call.data)
        _ensure_programmable(data)
        channel = int(call.data[ATTR_CHANNEL]) - 1
        _validate_pump_channel(data, channel)
        active, compensate = bool(call.data[ATTR_ENABLE]), bool(call.data[ATTR_COMPENSATE])
        async with data.dosing_programming.channel_lock(channel):
            if active and is_legacy_dosing_pump(data.device):
                await _resume_legacy_channel(hass, data, channel, compensate=compensate)
                return
            await data.device.set_channel_active(channel, active=active, compensate=compensate)
            setup = {"active": active, "compensate": compensate}
            await _record_and_mirror(hass, data, channel, setup, stamp_programmed=False)

    hass.services.async_register(
        DOMAIN, SERVICE_SET_DOSING_SCHEDULE, async_set_dosing_schedule, schema=SET_DOSING_SCHEDULE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_CHANNEL_ACTIVE, async_set_channel_active, schema=SET_CHANNEL_ACTIVE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_DOSE_DELAY,
        functools.partial(_async_set_dose_delay, hass),
        schema=SET_DOSE_DELAY_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_DOSING_CHANNEL,
        functools.partial(_async_reset_dosing_channel, hass),
        schema=RESET_DOSING_CHANNEL_SCHEMA,
    )


def async_remove_pump_services(hass: HomeAssistant) -> None:
    """Remove the dosing pump programming services."""
    for service in (
        SERVICE_SET_DOSING_SCHEDULE,
        SERVICE_SET_CHANNEL_ACTIVE,
        SERVICE_SET_DOSE_DELAY,
        SERVICE_RESET_DOSING_CHANNEL,
    ):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)


def _resolve_config_entry(hass: HomeAssistant, device: object) -> ConfigEntry | None:
    """Find the config entry whose device is ``device`` (matched by address)."""
    address = getattr(device, "address", None)
    if address is None:
        return None
    return _config_entry_for_address(hass, str(address))


def _require_config_entry(hass: HomeAssistant, data: DosingChihirosData | StirrerChihirosData) -> ConfigEntry:
    """Return the config entry for a resolved device, raising when absent."""
    entry = _resolve_config_entry(hass, data.device)
    if entry is None:
        raise HomeAssistantError(f"Config entry for {data.device.name} not found")
    return entry


def _resolve_stirrer_target(hass: HomeAssistant, call: ServiceCall) -> tuple[StirrerChihirosData, ConfigEntry]:
    """Resolve a stirrer service target and its config entry."""
    data = resolve_service_device(hass, call.data)
    if not isinstance(data, StirrerChihirosData):
        raise HomeAssistantError(f"{data.device.name} is not a magnetic stirrer")
    return data, _require_config_entry(hass, data)


async def _async_set_stirrer_master(hass: HomeAssistant, call: ServiceCall) -> None:
    """Link a stirrer to a master pump (or unlink) and optionally mirror."""
    data, entry = _resolve_stirrer_target(hass, call)
    master = _resolve_master(hass, call.data)
    if master is None:
        stored = {key: value for key, value in entry.data.items() if key != ATTR_MASTER_ADDRESS}
    else:
        stored = {**entry.data, ATTR_MASTER_ADDRESS: master.device.address}
    hass.config_entries.async_update_entry(entry, data=stored)
    set_stirrer_pre_run_entities_enabled(
        hass, data.device.address, len(data.stirrer_states), enabled=master is not None
    )
    if master is not None and call.data[ATTR_MIRROR]:
        await async_mirror_pump_to_stirrer(master, data, delay=call.data.get(ATTR_DELAY))


async def _async_mirror_stirrer(hass: HomeAssistant, call: ServiceCall) -> None:
    """Replay the linked pump's programming onto the stirrer."""
    data, entry = _resolve_stirrer_target(hass, call)
    master_address = str(entry.data.get(ATTR_MASTER_ADDRESS, ""))
    if not master_address:
        raise HomeAssistantError(f"{data.device.name} has no master pump linked")
    master = resolve_service_device(hass, {ATTR_ADDRESS: master_address})
    if not isinstance(master, DosingChihirosData):
        raise HomeAssistantError(f"Linked master {master.device.name} is not a dosing pump")
    await async_mirror_pump_to_stirrer(master, data, delay=call.data.get(ATTR_DELAY))


def async_register_stirrer_link_services(hass: HomeAssistant) -> None:
    """Register the stirrer master/link services."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_STIRRER_MASTER,
        functools.partial(_async_set_stirrer_master, hass),
        schema=SET_STIRRER_MASTER_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_MIRROR_STIRRER,
        functools.partial(_async_mirror_stirrer, hass),
        schema=MIRROR_STIRRER_SCHEMA,
    )


def async_remove_stirrer_link_services(hass: HomeAssistant) -> None:
    """Remove the stirrer master/link services."""
    for service in (SERVICE_SET_STIRRER_MASTER, SERVICE_MIRROR_STIRRER):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
