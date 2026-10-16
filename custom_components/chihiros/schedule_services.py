"""Home Assistant schedule services."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, cast

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .models import ChihirosData
from .runtime import is_device_kind
from .service_utils import DEVICE_SELECTOR_SCHEMA, resolve_service_device
from .vendor.chihiros_led_control.devices.led import LedSchedule
from .vendor.chihiros_led_control.models import DeviceKind, LedSpec, supports_weekdays
from .vendor.chihiros_led_control.protocol.led import AUTO_POINT_MAX_MINUTES
from .vendor.chihiros_led_control.schedule_validation import (
    find_duplicate_schedule_weekdays,
    normalize_schedule_weekdays,
)
from .vendor.chihiros_led_control.weekday_encoding import WeekdaySelect

_LOGGER = logging.getLogger(__name__)

SERVICE_ADD_SCHEDULE = "add_schedule"
SERVICE_REMOVE_SCHEDULE = "remove_schedule"
SERVICE_RESET_SCHEDULE = "reset_schedule"
SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_SET_AUTO_CURVE = "set_auto_curve"

ATTR_BRIGHTNESS = "brightness"
ATTR_CURVE = "curve"
ATTR_END = "end"
ATTR_LEVELS = "levels"
ATTR_PERIODS = "periods"
ATTR_RAMP_UP_MINUTES = "ramp_up_minutes"
ATTR_START = "start"
ATTR_WEEKDAYS = "weekdays"

WEEKDAY_VALUES = [weekday.value for weekday in WeekdaySelect]
BRIGHTNESS_VALUE_SCHEMA = vol.All(vol.Coerce(int), vol.Range(min=0, max=100))
LEVELS_SCHEMA = {str: BRIGHTNESS_VALUE_SCHEMA}
SCHEDULE_PERIOD_SCHEMA = {
    vol.Required(ATTR_START): str,
    vol.Required(ATTR_END): str,
    vol.Optional(ATTR_BRIGHTNESS, default=100): vol.Any(BRIGHTNESS_VALUE_SCHEMA, LEVELS_SCHEMA),
    vol.Optional(ATTR_LEVELS): LEVELS_SCHEMA,
    vol.Optional(ATTR_RAMP_UP_MINUTES, default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
    vol.Optional(ATTR_WEEKDAYS): vol.All(list, [vol.In(WEEKDAY_VALUES)]),
}
ADD_SCHEDULE_SCHEMA = vol.Schema({**DEVICE_SELECTOR_SCHEMA, **SCHEDULE_PERIOD_SCHEMA})
REMOVE_SCHEDULE_SCHEMA = vol.Schema(
    {
        **DEVICE_SELECTOR_SCHEMA,
        vol.Required(ATTR_START): str,
        vol.Required(ATTR_END): str,
        vol.Optional(ATTR_RAMP_UP_MINUTES, default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
        vol.Optional(ATTR_WEEKDAYS): vol.All(list, [vol.In(WEEKDAY_VALUES)]),
    }
)
RESET_SCHEDULE_SCHEMA = vol.Schema(DEVICE_SELECTOR_SCHEMA)
SET_SCHEDULE_SCHEMA = vol.Schema(
    {**DEVICE_SELECTOR_SCHEMA, vol.Required(ATTR_PERIODS): vol.All(list, [vol.Schema(SCHEDULE_PERIOD_SCHEMA)])}
)
# Auto-curve points are per-channel [minutes, level] pairs; minutes are since midnight.
AUTO_CURVE_POINT_SCHEMA = vol.All([vol.Coerce(int)], vol.Length(min=2, max=2))
SET_AUTO_CURVE_SCHEMA = vol.Schema(
    {
        **DEVICE_SELECTOR_SCHEMA,
        vol.Required(ATTR_CURVE): {vol.Coerce(int): vol.All(list, [AUTO_CURVE_POINT_SCHEMA])},
    }
)


async def _async_add_schedule_service(call: ServiceCall) -> None:
    """Add one schedule period to the selected light."""
    data = resolve_service_device(call.hass, call.data)
    ensure_light_device(data)
    validate_schedule_period(data, call.data)
    await async_add_schedule_period(data, call.data)
    await async_refresh_status(data)


async def _async_remove_schedule_service(call: ServiceCall) -> None:
    """Remove one schedule period from the selected light."""
    data = resolve_service_device(call.hass, call.data)
    ensure_light_device(data)
    start = parse_schedule_time(call.data[ATTR_START])
    end = parse_schedule_time(call.data[ATTR_END])
    validate_time_range(start, end)
    await data.device.remove_setting(
        start,
        end,
        ramp_up_in_minutes=call.data[ATTR_RAMP_UP_MINUTES],
        weekdays=parse_weekdays(call.data.get(ATTR_WEEKDAYS)),
    )
    await async_refresh_status(data)


async def _async_reset_schedule_service(call: ServiceCall) -> None:
    """Remove all schedule periods from the selected light."""
    data = resolve_service_device(call.hass, call.data)
    ensure_light_device(data)
    await data.device.reset_settings()
    await async_refresh_status(data)


async def _async_set_schedule_service(call: ServiceCall) -> None:
    """Replace the full schedule of the selected light."""
    data = resolve_service_device(call.hass, call.data)
    ensure_light_device(data)
    validate_schedule_periods(data, call.data[ATTR_PERIODS])
    await async_replace_schedule(data, call.data[ATTR_PERIODS])
    data.coordinator.async_set_auto_mode(True)
    await async_refresh_status(data)


async def _async_rollback_partial_curve(data: ChihirosData) -> None:
    """Best-effort clear of a partially written auto curve."""
    try:
        await data.device.reset_settings()
    except Exception:
        _LOGGER.exception("Failed to clear a partial auto curve for %s", data.device.name)


async def _async_write_auto_curve(data: ChihirosData, points: list[tuple[int, int, int]]) -> None:
    """Write auto curve points, rolling back a partial replacement on failure."""
    try:
        await data.device.set_auto_curve(points)
    except ValueError as ex:
        raise HomeAssistantError(str(ex)) from ex
    except Exception as ex:
        await _async_rollback_partial_curve(data)
        raise HomeAssistantError(
            f"Failed to write auto curve for {data.device.name}; "
            "the previous curve cannot be restored and the integration "
            "attempted to clear the partial replacement"
        ) from ex


async def _async_set_auto_curve_service(call: ServiceCall) -> None:
    """Replace the device's auto curve with 0x5A/0x06 points (app format)."""
    data = resolve_service_device(call.hass, call.data)
    ensure_light_device(data)
    curve = call.data[ATTR_CURVE]
    validate_auto_curve(data, curve)
    points = [
        (channel, minutes, cast(LedSpec, data.device.model.spec).percent_to_level(channel, level))
        for channel, channel_points in curve.items()
        for minutes, level in channel_points
    ]
    await _async_write_auto_curve(data, points)
    data.coordinator.async_set_auto_mode(True)
    await async_refresh_status(data)


_SCHEDULE_SERVICE_REGISTRATIONS = (
    (SERVICE_ADD_SCHEDULE, _async_add_schedule_service, ADD_SCHEDULE_SCHEMA),
    (SERVICE_REMOVE_SCHEDULE, _async_remove_schedule_service, REMOVE_SCHEDULE_SCHEMA),
    (SERVICE_RESET_SCHEDULE, _async_reset_schedule_service, RESET_SCHEDULE_SCHEMA),
    (SERVICE_SET_SCHEDULE, _async_set_schedule_service, SET_SCHEDULE_SCHEMA),
    (SERVICE_SET_AUTO_CURVE, _async_set_auto_curve_service, SET_AUTO_CURVE_SCHEMA),
)


def async_register_schedule_services(hass: HomeAssistant) -> None:
    """Register schedule services for configured light devices."""
    for service, handler, schema in _SCHEDULE_SERVICE_REGISTRATIONS:
        if not hass.services.has_service(DOMAIN, service):
            hass.services.async_register(DOMAIN, service, handler, schema=schema)


def async_remove_schedule_services(hass: HomeAssistant) -> None:
    """Remove all registered schedule services."""
    for service in (
        SERVICE_ADD_SCHEDULE,
        SERVICE_REMOVE_SCHEDULE,
        SERVICE_RESET_SCHEDULE,
        SERVICE_SET_SCHEDULE,
        SERVICE_SET_AUTO_CURVE,
    ):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)


def ensure_light_device(chihiros_data: ChihirosData) -> None:
    """Validate that the selected service target is a light."""
    if not is_device_kind(chihiros_data.device, DeviceKind.LED):
        raise HomeAssistantError(f"{chihiros_data.device.name} is not a light")


def validate_schedule_periods(chihiros_data: ChihirosData, periods: list[dict[str, Any]]) -> None:
    """Validate a full replacement schedule before writing anything."""
    if not periods:
        raise HomeAssistantError("Schedule must contain at least one period")
    validated = [validate_schedule_period(chihiros_data, period) for period in periods]
    if duplicate := find_duplicate_schedule_weekdays([period["weekdays"] for period in validated]):
        weekdays = ", ".join(weekday.value for weekday in duplicate.weekdays)
        raise HomeAssistantError(
            f"{chihiros_data.device.name} stores only one schedule period per weekday; "
            f"periods {duplicate.first_index + 1} and {duplicate.second_index + 1} both target {weekdays}"
        )


def validate_schedule_period(chihiros_data: ChihirosData, data: dict[str, Any]) -> dict[str, Any]:
    """Validate one schedule period against the selected device."""
    start = parse_schedule_time(data[ATTR_START])
    end = parse_schedule_time(data[ATTR_END])
    validate_time_range(start, end)
    validate_schedule_brightness(chihiros_data, data)
    weekdays = normalize_schedule_weekdays(parse_weekdays(data.get(ATTR_WEEKDAYS)))
    device = chihiros_data.device
    if (not device.model.supports_weekdays or not supports_weekdays(device.name)) and len(weekdays) != 7:
        raise HomeAssistantError(f"{device.name} does not support weekday selection")
    return {"weekdays": weekdays}


def validate_time_range(start: datetime, end: datetime) -> None:
    """Treat only midnight as the scene endpoint 23:59."""
    effective_end = end.replace(hour=23, minute=59) if end.hour == 0 and end.minute == 0 else end
    if start >= effective_end:
        raise HomeAssistantError("Schedule start time must be before end time")


def _validate_schedule_percentages(brightness: int | dict[str, int]) -> None:
    """Keep percentage service inputs separate from raw model-unit limits."""
    percentages = [brightness] if isinstance(brightness, int) else brightness.values()
    if any(not 0 <= level <= 100 for level in percentages):
        raise HomeAssistantError("Schedule brightness percentages must be between 0 and 100")


def validate_schedule_brightness(chihiros_data: ChihirosData, data: dict[str, Any]) -> None:
    """Validate schedule channel levels against the device model."""
    supported = set(chihiros_data.device.colors)
    brightness = brightness_from_service_data(data)
    _validate_schedule_percentages(brightness)
    if isinstance(brightness, int):
        if not supported:
            raise HomeAssistantError(f"{chihiros_data.device.name} does not expose any controllable channels")
        return
    if not brightness:
        raise HomeAssistantError("Schedule levels must contain at least one channel")
    if unsupported := set(brightness) - supported:
        raise HomeAssistantError(
            f"Channel {', '.join(sorted(unsupported))} is not supported by {chihiros_data.device.name}. "
            f"Supported channels: {', '.join(sorted(supported))}"
        )


async def async_add_schedule_period(chihiros_data: ChihirosData, data: dict[str, Any]) -> None:
    """Add one auto schedule period."""
    await chihiros_data.device.add_setting(
        parse_schedule_time(data[ATTR_START]),
        parse_schedule_time(data[ATTR_END]),
        max_brightness=model_brightness_from_service_data(chihiros_data, data),
        ramp_up_in_minutes=data[ATTR_RAMP_UP_MINUTES],
        weekdays=parse_weekdays(data.get(ATTR_WEEKDAYS)),
    )


async def async_replace_schedule(chihiros_data: ChihirosData, periods: list[dict[str, Any]]) -> None:
    """Replace a schedule and clear any partial replacement after failure."""
    settings = [
        LedSchedule(
            sunrise=parse_schedule_time(period[ATTR_START]),
            sunset=parse_schedule_time(period[ATTR_END]),
            max_brightness=model_brightness_from_service_data(chihiros_data, period),
            ramp_up_in_minutes=period[ATTR_RAMP_UP_MINUTES],
            weekdays=parse_weekdays(period.get(ATTR_WEEKDAYS)),
        )
        for period in periods
    ]
    try:
        await chihiros_data.device.replace_settings(settings)
    except ValueError as ex:
        # Validation failed before any device mutation; never clear an unknown schedule.
        raise HomeAssistantError(str(ex)) from ex
    except Exception as ex:
        try:
            await chihiros_data.device.reset_settings()
        except Exception:
            _LOGGER.exception("Failed to clear a partial schedule for %s", chihiros_data.device.name)
        raise HomeAssistantError(
            f"Failed to replace the schedule for {chihiros_data.device.name}; "
            "the previous schedule cannot be restored and the integration attempted to clear the partial replacement"
        ) from ex


def _validate_curve_channels(chihiros_data: ChihirosData, curve: dict[int, list[list[int]]]) -> int:
    """Validate that every curve channel is controllable; return the channel count."""
    channels = chihiros_data.device.colors
    if not channels:
        raise HomeAssistantError(f"{chihiros_data.device.name} does not expose any controllable channels")
    channel_count = max(channels.values()) + 1
    if unsupported := sorted(set(curve) - set(channels.values())):
        raise HomeAssistantError(
            f"Channel {', '.join(str(channel) for channel in unsupported)} is not supported by "
            f"{chihiros_data.device.name}; supported channel ids: "
            f"{', '.join(str(i) for i in sorted(channels.values()))}"
        )
    return channel_count


def _validate_curve_channel_points(chihiros_data: ChihirosData, channel: int, points: list[list[int]]) -> None:
    """Validate minutes and percentage levels (not raw model units)."""
    if not points:
        raise HomeAssistantError(
            f"Auto curve channel {channel} for {chihiros_data.device.name} must contain at least one point"
        )
    for minutes, level in points:
        if not 0 <= minutes <= AUTO_POINT_MAX_MINUTES:
            raise HomeAssistantError(
                f"Auto curve point minutes must be between 0 and {AUTO_POINT_MAX_MINUTES} for "
                f"{chihiros_data.device.name}"
            )
        if not 0 <= level <= 100:
            raise HomeAssistantError(
                f"Auto curve point level must be between 0 and 100 for {chihiros_data.device.name}"
            )


def validate_auto_curve(chihiros_data: ChihirosData, curve: dict[int, list[list[int]]]) -> None:
    """Validate percentage auto-curve points before any destructive writes."""
    if not curve:
        raise HomeAssistantError("Auto curve must contain at least one channel with at least one point")
    _validate_curve_channels(chihiros_data, curve)
    for channel, points in curve.items():
        _validate_curve_channel_points(chihiros_data, channel, points)


def parse_schedule_time(value: str) -> datetime:
    """Parse an HH:MM schedule value."""
    try:
        parsed_time = datetime.strptime(value, "%H:%M").time()
    except ValueError as ex:
        raise HomeAssistantError(f"Invalid schedule time {value!r}; expected HH:MM") from ex
    return datetime.combine(date.today(), parsed_time)


def brightness_from_service_data(data: dict[str, Any]) -> int | dict[str, int]:
    """Return existing service brightness/levels as percentages, not raw model units."""
    if ATTR_LEVELS in data:
        return dict(data[ATTR_LEVELS])
    brightness = data[ATTR_BRIGHTNESS]
    return dict(brightness) if isinstance(brightness, dict) else brightness


def model_brightness_from_service_data(chihiros_data: ChihirosData, data: dict[str, Any]) -> int | dict[str, int]:
    """Convert service percentages channel-wise to the public driver's raw units."""
    brightness = brightness_from_service_data(data)
    channels = chihiros_data.device.colors
    spec = cast(LedSpec, chihiros_data.device.model.spec)
    if isinstance(brightness, int):
        primary = "white" if "white" in channels else min(channels, key=channels.__getitem__)
        return spec.percent_to_level(channels[primary], brightness)
    return {color: spec.percent_to_level(channels[color], level) for color, level in brightness.items()}


def parse_weekdays(value: list[str] | None) -> list[WeekdaySelect] | None:
    """Parse service weekday strings."""
    return None if value is None else [WeekdaySelect(weekday) for weekday in value]


async def async_refresh_status(chihiros_data: ChihirosData) -> None:
    """Refresh schedule sensors after a schedule write."""
    try:
        await chihiros_data.coordinator.async_request_status()
    except Exception:
        _LOGGER.debug("Failed to refresh Chihiros status after schedule write", exc_info=True)
