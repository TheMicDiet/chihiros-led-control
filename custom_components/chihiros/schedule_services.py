"""Home Assistant schedule services."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .models import ChihirosData
from .service_utils import DEVICE_SELECTOR_SCHEMA, resolve_service_device
from .vendor.chihiros_led_control.commands import AUTO_POINT_MAX_MINUTES
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
# Auto-curve points use the app's 0x5A/0x06 per-point encoding: each point is a
# [minutes, level] pair for one channel. ``minutes`` is minutes since midnight.
AUTO_CURVE_POINT_SCHEMA = vol.All([vol.Coerce(int)], vol.Length(min=2, max=2))
SET_AUTO_CURVE_SCHEMA = vol.Schema(
    {
        **DEVICE_SELECTOR_SCHEMA,
        vol.Required(ATTR_CURVE): {vol.Coerce(int): vol.All(list, [AUTO_CURVE_POINT_SCHEMA])},
    }
)


def async_register_schedule_services(hass: HomeAssistant) -> None:
    """Register schedule services for configured light devices."""

    async def async_add_schedule(call: ServiceCall) -> None:
        data = resolve_service_device(hass, call.data)
        ensure_light_device(data)
        validate_schedule_period(data, call.data)
        await async_add_schedule_period(data, call.data)
        await async_refresh_status(data)

    async def async_remove_schedule(call: ServiceCall) -> None:
        data = resolve_service_device(hass, call.data)
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

    async def async_reset_schedule(call: ServiceCall) -> None:
        data = resolve_service_device(hass, call.data)
        ensure_light_device(data)
        await data.device.reset_settings()
        await async_refresh_status(data)

    async def async_set_schedule(call: ServiceCall) -> None:
        data = resolve_service_device(hass, call.data)
        ensure_light_device(data)
        validate_schedule_periods(data, call.data[ATTR_PERIODS])
        await async_replace_schedule(data, call.data[ATTR_PERIODS])
        await async_refresh_status(data)

    async def async_set_auto_curve(call: ServiceCall) -> None:
        """Replace the device's auto curve with 0x5A/0x06 points (app format)."""
        data = resolve_service_device(hass, call.data)
        ensure_light_device(data)
        curve = call.data[ATTR_CURVE]
        validate_auto_curve(data, curve)
        points = [
            (channel, minutes, level) for channel, channel_points in curve.items() for minutes, level in channel_points
        ]
        try:
            await data.device.reset_settings()
        except Exception as ex:
            raise HomeAssistantError(
                f"Failed to start auto curve update for {data.device.name}; the existing curve may remain"
            ) from ex
        try:
            await data.device.set_auto_curve(points)
        except Exception as ex:
            try:
                await data.device.reset_settings()
            except Exception:
                _LOGGER.exception("Failed to clear a partial auto curve for %s", data.device.name)
            raise HomeAssistantError(
                f"Failed to write auto curve for {data.device.name}; "
                "the previous curve cannot be restored and the integration "
                "attempted to clear the partial replacement"
            ) from ex
        await async_refresh_status(data)

    registrations = (
        (SERVICE_ADD_SCHEDULE, async_add_schedule, ADD_SCHEDULE_SCHEMA),
        (SERVICE_REMOVE_SCHEDULE, async_remove_schedule, REMOVE_SCHEDULE_SCHEMA),
        (SERVICE_RESET_SCHEDULE, async_reset_schedule, RESET_SCHEDULE_SCHEMA),
        (SERVICE_SET_SCHEDULE, async_set_schedule, SET_SCHEDULE_SCHEMA),
        (SERVICE_SET_AUTO_CURVE, async_set_auto_curve, SET_AUTO_CURVE_SCHEMA),
    )
    for service, handler, schema in registrations:
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
    if chihiros_data.dosing_totals:
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
    return {"weekdays": normalize_schedule_weekdays(parse_weekdays(data.get(ATTR_WEEKDAYS)))}


def validate_time_range(start: datetime, end: datetime) -> None:
    """Validate schedule start/end ordering."""
    if start >= end:
        raise HomeAssistantError("Schedule start time must be before end time")


def validate_schedule_brightness(chihiros_data: ChihirosData, data: dict[str, Any]) -> None:
    """Validate schedule channel levels against the device model."""
    supported = set(chihiros_data.device.colors)
    brightness = brightness_from_service_data(data)
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
        max_brightness=brightness_from_service_data(data),
        ramp_up_in_minutes=data[ATTR_RAMP_UP_MINUTES],
        weekdays=parse_weekdays(data.get(ATTR_WEEKDAYS)),
    )


async def async_replace_schedule(chihiros_data: ChihirosData, periods: list[dict[str, Any]]) -> None:
    """Replace a schedule and clear any partial replacement after failure."""
    try:
        await chihiros_data.device.reset_settings()
    except Exception as ex:
        raise HomeAssistantError(
            f"Failed to start schedule replacement for {chihiros_data.device.name}; the existing schedule may remain"
        ) from ex
    try:
        for period in periods:
            await async_add_schedule_period(chihiros_data, period)
    except Exception as ex:
        try:
            await chihiros_data.device.reset_settings()
        except Exception:
            _LOGGER.exception("Failed to clear a partial schedule for %s", chihiros_data.device.name)
        raise HomeAssistantError(
            f"Failed to replace the schedule for {chihiros_data.device.name}; "
            "the previous schedule cannot be restored and the integration attempted to clear the partial replacement"
        ) from ex


def validate_auto_curve(chihiros_data: ChihirosData, curve: dict[int, list[list[int]]]) -> None:
    """Validate auto-curve points against the selected device before writing."""
    if not curve:
        raise HomeAssistantError("Auto curve must contain at least one channel with at least one point")
    channels = chihiros_data.device.colors
    if not channels:
        raise HomeAssistantError(f"{chihiros_data.device.name} does not expose any controllable channels")
    channel_count = max(channels.values()) + 1
    if unsupported := sorted({channel for channel in curve if not 0 <= channel < channel_count}):
        raise HomeAssistantError(
            f"Channel {', '.join(str(channel) for channel in unsupported)} is not supported by "
            f"{chihiros_data.device.name}; supported channel ids: {', '.join(str(i) for i in range(channel_count))}"
        )
    for channel, points in curve.items():
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


def parse_schedule_time(value: str) -> datetime:
    """Parse an HH:MM schedule value."""
    try:
        parsed_time = datetime.strptime(value, "%H:%M").time()
    except ValueError as ex:
        raise HomeAssistantError(f"Invalid schedule time {value!r}; expected HH:MM") from ex
    return datetime.combine(date.today(), parsed_time)


def brightness_from_service_data(data: dict[str, Any]) -> int | dict[str, int]:
    """Return brightness data accepted by the runtime client."""
    if ATTR_LEVELS in data:
        return dict(data[ATTR_LEVELS])
    brightness = data[ATTR_BRIGHTNESS]
    return dict(brightness) if isinstance(brightness, dict) else brightness


def parse_weekdays(value: list[str] | None) -> list[WeekdaySelect] | None:
    """Parse service weekday strings."""
    return None if value is None else [WeekdaySelect(weekday) for weekday in value]


async def async_refresh_status(chihiros_data: ChihirosData) -> None:
    """Refresh schedule sensors after a schedule write."""
    try:
        await chihiros_data.coordinator.async_request_status()
    except Exception:
        _LOGGER.debug("Failed to refresh Chihiros status after schedule write", exc_info=True)
