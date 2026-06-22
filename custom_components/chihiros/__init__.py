"""Chihiros HA integration root module."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, cast

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .coordinator import ChihirosDataUpdateCoordinator
from .dosing import CONF_PUMP_COUNT, DosingDailyTotals, is_dosing_capable, normalize_pump_count
from .models import ChihirosData
from .runtime import DosingChihirosClient, resolve_chihiros_runtime
from .vendor.chihiros_led_control.schedule_validation import (
    find_duplicate_schedule_weekdays,
    normalize_schedule_weekdays,
)
from .vendor.chihiros_led_control.weekday_encoding import WeekdaySelect

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.SWITCH, Platform.SENSOR, Platform.NUMBER, Platform.BUTTON]

SERVICE_ADD_SCHEDULE = "add_schedule"
SERVICE_REMOVE_SCHEDULE = "remove_schedule"
SERVICE_RESET_SCHEDULE = "reset_schedule"
SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_DOSE_ML = "dose_ml"

ATTR_ADDRESS = "address"
ATTR_BRIGHTNESS = "brightness"
ATTR_END = "end"
ATTR_ENTRY_ID = "entry_id"
ATTR_LEVELS = "levels"
ATTR_ML = "ml"
ATTR_PERIODS = "periods"
ATTR_PUMP = "pump"
ATTR_RAMP_UP_MINUTES = "ramp_up_minutes"
ATTR_START = "start"
ATTR_WEEKDAYS = "weekdays"

WEEKDAY_VALUES = [weekday.value for weekday in WeekdaySelect]

BRIGHTNESS_VALUE_SCHEMA = vol.All(vol.Coerce(int), vol.Range(min=0, max=100))
LEVELS_SCHEMA = {str: BRIGHTNESS_VALUE_SCHEMA}
SCHEDULE_SELECTOR_SCHEMA = {
    vol.Optional(ATTR_ENTRY_ID): str,
    vol.Optional(ATTR_ADDRESS): str,
}
SCHEDULE_PERIOD_SCHEMA = {
    vol.Required(ATTR_START): str,
    vol.Required(ATTR_END): str,
    vol.Optional(ATTR_BRIGHTNESS, default=100): vol.Any(BRIGHTNESS_VALUE_SCHEMA, LEVELS_SCHEMA),
    vol.Optional(ATTR_LEVELS): LEVELS_SCHEMA,
    vol.Optional(ATTR_RAMP_UP_MINUTES, default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
    vol.Optional(ATTR_WEEKDAYS): vol.All(list, [vol.In(WEEKDAY_VALUES)]),
}
ADD_SCHEDULE_SCHEMA = vol.Schema({**SCHEDULE_SELECTOR_SCHEMA, **SCHEDULE_PERIOD_SCHEMA})
REMOVE_SCHEDULE_SCHEMA = vol.Schema(
    {
        **SCHEDULE_SELECTOR_SCHEMA,
        vol.Required(ATTR_START): str,
        vol.Required(ATTR_END): str,
        vol.Optional(ATTR_RAMP_UP_MINUTES, default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
        vol.Optional(ATTR_WEEKDAYS): vol.All(list, [vol.In(WEEKDAY_VALUES)]),
    }
)
RESET_SCHEDULE_SCHEMA = vol.Schema(SCHEDULE_SELECTOR_SCHEMA)
SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        **SCHEDULE_SELECTOR_SCHEMA,
        vol.Required(ATTR_PERIODS): vol.All(list, [vol.Schema(SCHEDULE_PERIOD_SCHEMA)]),
    }
)
DOSE_ML_SCHEMA = vol.Schema(
    {
        **SCHEDULE_SELECTOR_SCHEMA,
        vol.Required(ATTR_PUMP): vol.All(vol.Coerce(int), vol.Range(min=1, max=4)),
        vol.Required(ATTR_ML): vol.All(vol.Coerce(float), vol.Range(min=0.2, max=999.9)),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up chihiros from a config entry."""
    runtime = await resolve_chihiros_runtime(hass, entry)
    coordinator = ChihirosDataUpdateCoordinator(
        hass,
        runtime.client,
        runtime.address,
        always_available=runtime.always_available,
    )
    coordinator.async_start_bluetooth()

    dosing_totals = None
    dosing_volumes: list[float] = []
    if is_dosing_capable(runtime.client):
        dosing_totals = DosingDailyTotals(hass, runtime.address, normalize_pump_count(entry.data.get(CONF_PUMP_COUNT)))
        await dosing_totals.async_load()
        dosing_volumes = [1.0] * dosing_totals.pump_count

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = ChihirosData(
        entry.title, runtime.client, coordinator, dosing_totals, dosing_volumes
    )
    _async_update_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        chihiros_data: ChihirosData = hass.data[DOMAIN].pop(entry.entry_id)
        chihiros_data.coordinator.async_close()
        if chihiros_data.dosing_totals:
            chihiros_data.dosing_totals.async_close()
        await chihiros_data.device.disconnect()
        _async_update_services(hass)

    return unload_ok


def _async_update_services(hass: HomeAssistant) -> None:
    """Register services that apply to currently configured device capabilities."""
    has_light_device = _has_light_devices(hass)
    has_dosing_device = _has_dosing_devices(hass)

    if has_light_device:
        _async_register_services(hass)
    else:
        _async_remove_schedule_services(hass)

    if has_dosing_device:
        _async_register_dosing_service(hass)
    else:
        _async_remove_dosing_service(hass)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register schedule management services once."""

    async def async_add_schedule(call: ServiceCall) -> None:
        chihiros_data = _resolve_service_device(hass, call.data)
        _ensure_light_device(chihiros_data)
        _validate_schedule_period(chihiros_data, call.data)
        await _async_add_schedule_period(chihiros_data, call.data)
        await _async_refresh_status(chihiros_data)

    async def async_remove_schedule(call: ServiceCall) -> None:
        chihiros_data = _resolve_service_device(hass, call.data)
        _ensure_light_device(chihiros_data)
        start = _parse_schedule_time(call.data[ATTR_START])
        end = _parse_schedule_time(call.data[ATTR_END])
        _validate_time_range(start, end)
        await chihiros_data.device.remove_setting(
            start,
            end,
            ramp_up_in_minutes=call.data[ATTR_RAMP_UP_MINUTES],
            weekdays=_parse_weekdays(call.data.get(ATTR_WEEKDAYS)),
        )
        await _async_refresh_status(chihiros_data)

    async def async_reset_schedule(call: ServiceCall) -> None:
        chihiros_data = _resolve_service_device(hass, call.data)
        _ensure_light_device(chihiros_data)
        await chihiros_data.device.reset_settings()
        await _async_refresh_status(chihiros_data)

    async def async_set_schedule(call: ServiceCall) -> None:
        chihiros_data = _resolve_service_device(hass, call.data)
        _ensure_light_device(chihiros_data)
        _validate_schedule_periods(chihiros_data, call.data[ATTR_PERIODS])
        await chihiros_data.device.reset_settings()
        for period in call.data[ATTR_PERIODS]:
            await _async_add_schedule_period(chihiros_data, period)
        await _async_refresh_status(chihiros_data)

    if not hass.services.has_service(DOMAIN, SERVICE_ADD_SCHEDULE):
        hass.services.async_register(DOMAIN, SERVICE_ADD_SCHEDULE, async_add_schedule, schema=ADD_SCHEDULE_SCHEMA)
    if not hass.services.has_service(DOMAIN, SERVICE_REMOVE_SCHEDULE):
        hass.services.async_register(
            DOMAIN, SERVICE_REMOVE_SCHEDULE, async_remove_schedule, schema=REMOVE_SCHEDULE_SCHEMA
        )
    if not hass.services.has_service(DOMAIN, SERVICE_RESET_SCHEDULE):
        hass.services.async_register(DOMAIN, SERVICE_RESET_SCHEDULE, async_reset_schedule, schema=RESET_SCHEDULE_SCHEMA)
    if not hass.services.has_service(DOMAIN, SERVICE_SET_SCHEDULE):
        hass.services.async_register(DOMAIN, SERVICE_SET_SCHEDULE, async_set_schedule, schema=SET_SCHEDULE_SCHEMA)


def _async_register_dosing_service(hass: HomeAssistant) -> None:
    """Register dosing service once."""
    if hass.services.has_service(DOMAIN, SERVICE_DOSE_ML):
        return

    async def async_dose_ml(call: ServiceCall) -> None:
        chihiros_data = _resolve_service_device(hass, call.data)
        if not chihiros_data.dosing_totals:
            raise HomeAssistantError(f"{chihiros_data.device.name} is not a dosing pump")
        pump_idx = int(call.data[ATTR_PUMP]) - 1
        if pump_idx >= chihiros_data.dosing_totals.pump_count:
            raise HomeAssistantError(f"{chihiros_data.device.name} has {chihiros_data.dosing_totals.pump_count} pumps")
        volume_ml = float(call.data[ATTR_ML])
        await async_trigger_dose_ml(chihiros_data, pump_idx, volume_ml)

    hass.services.async_register(DOMAIN, SERVICE_DOSE_ML, async_dose_ml, schema=DOSE_ML_SCHEMA)


def _async_remove_schedule_services(hass: HomeAssistant) -> None:
    """Remove light schedule services if they are registered."""
    for service in (SERVICE_ADD_SCHEDULE, SERVICE_REMOVE_SCHEDULE, SERVICE_RESET_SCHEDULE, SERVICE_SET_SCHEDULE):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)


def _async_remove_dosing_service(hass: HomeAssistant) -> None:
    """Remove dosing service if it is registered."""
    if hass.services.has_service(DOMAIN, SERVICE_DOSE_ML):
        hass.services.async_remove(DOMAIN, SERVICE_DOSE_ML)


async def async_trigger_dose_ml(chihiros_data: ChihirosData, pump_idx: int, volume_ml: float) -> None:
    """Trigger a manual dose and update local totals."""
    if not chihiros_data.dosing_totals:
        raise HomeAssistantError(f"{chihiros_data.device.name} is not a dosing pump")
    dosing_device = cast(DosingChihirosClient, chihiros_data.device)
    await dosing_device.dose_ml(pump_idx, volume_ml)
    await chihiros_data.dosing_totals.async_add_dose(pump_idx, volume_ml)


def _has_light_devices(hass: HomeAssistant) -> bool:
    """Return whether any configured device supports light services."""
    return any(data.device.colors for data in hass.data.get(DOMAIN, {}).values())


def _has_dosing_devices(hass: HomeAssistant) -> bool:
    """Return whether any configured device supports dosing services."""
    return any(data.dosing_totals for data in hass.data.get(DOMAIN, {}).values())


def _resolve_service_device(hass: HomeAssistant, data: dict[str, Any]) -> ChihirosData:
    """Resolve a service call to one configured Chihiros device."""
    entries: dict[str, ChihirosData] = hass.data.get(DOMAIN, {})
    if entry_id := data.get(ATTR_ENTRY_ID):
        if entry_id in entries:
            return entries[entry_id]
        raise HomeAssistantError(f"Chihiros config entry not found: {entry_id}")

    if address := data.get(ATTR_ADDRESS):
        normalized_address = address.upper()
        for chihiros_data in entries.values():
            if chihiros_data.device.address.upper() == normalized_address:
                return chihiros_data
        raise HomeAssistantError(f"Chihiros device address not found: {address}")

    if len(entries) == 1:
        return next(iter(entries.values()))
    raise HomeAssistantError("Multiple Chihiros devices are configured; provide entry_id or address")


def _ensure_light_device(chihiros_data: ChihirosData) -> None:
    """Validate that the selected service target is a light."""
    if chihiros_data.dosing_totals:
        raise HomeAssistantError(f"{chihiros_data.device.name} is not a light")


def _validate_schedule_periods(chihiros_data: ChihirosData, periods: list[dict[str, Any]]) -> None:
    """Validate a full replacement schedule before writing anything to the device."""
    if not periods:
        raise HomeAssistantError("Schedule must contain at least one period")
    validated_periods = [_validate_schedule_period(chihiros_data, period) for period in periods]
    if duplicate := find_duplicate_schedule_weekdays([period["weekdays"] for period in validated_periods]):
        weekdays = ", ".join(weekday.value for weekday in duplicate.weekdays)
        raise HomeAssistantError(
            f"{chihiros_data.device.name} stores only one schedule period per weekday; "
            f"periods {duplicate.first_index + 1} and {duplicate.second_index + 1} both target {weekdays}"
        )


def _validate_schedule_period(chihiros_data: ChihirosData, data: dict[str, Any]) -> dict[str, Any]:
    """Validate one schedule period against the selected device."""
    start = _parse_schedule_time(data[ATTR_START])
    end = _parse_schedule_time(data[ATTR_END])
    _validate_time_range(start, end)
    _validate_schedule_brightness(chihiros_data, data)
    weekdays = normalize_schedule_weekdays(_parse_weekdays(data.get(ATTR_WEEKDAYS)))
    return {
        "weekdays": weekdays,
    }


def _validate_time_range(start: datetime, end: datetime) -> None:
    """Validate schedule start/end ordering."""
    if start >= end:
        raise HomeAssistantError("Schedule start time must be before end time")


def _validate_schedule_brightness(chihiros_data: ChihirosData, data: dict[str, Any]) -> None:
    """Validate schedule channel levels against the device model."""
    supported_channels = set(chihiros_data.device.colors)
    brightness = _brightness_from_service_data(data)
    if isinstance(brightness, int):
        if not supported_channels:
            raise HomeAssistantError(f"{chihiros_data.device.name} does not expose any controllable channels")
        return
    if not brightness:
        raise HomeAssistantError("Schedule levels must contain at least one channel")
    requested_channels = set(brightness)
    unsupported_channels = requested_channels - supported_channels
    if unsupported_channels:
        unsupported = ", ".join(sorted(unsupported_channels))
        supported = ", ".join(sorted(supported_channels))
        raise HomeAssistantError(
            f"Channel {unsupported} is not supported by {chihiros_data.device.name}. Supported channels: {supported}"
        )


async def _async_add_schedule_period(chihiros_data: ChihirosData, data: dict[str, Any]) -> None:
    """Add one auto schedule period."""
    start = _parse_schedule_time(data[ATTR_START])
    end = _parse_schedule_time(data[ATTR_END])
    await chihiros_data.device.add_setting(
        start,
        end,
        max_brightness=_brightness_from_service_data(data),
        ramp_up_in_minutes=data[ATTR_RAMP_UP_MINUTES],
        weekdays=_parse_weekdays(data.get(ATTR_WEEKDAYS)),
    )


def _parse_schedule_time(value: str) -> datetime:
    """Parse an HH:MM schedule value into a datetime accepted by the runtime client."""
    try:
        parsed_time = datetime.strptime(value, "%H:%M").time()
    except ValueError as ex:
        raise HomeAssistantError(f"Invalid schedule time {value!r}; expected HH:MM") from ex
    return datetime.combine(date.today(), parsed_time)


def _brightness_from_service_data(data: dict[str, Any]) -> int | dict[str, int]:
    """Return brightness data accepted by the runtime client."""
    if ATTR_LEVELS in data:
        return dict(data[ATTR_LEVELS])
    brightness = data[ATTR_BRIGHTNESS]
    if isinstance(brightness, dict):
        return dict(brightness)
    return brightness


def _parse_weekdays(value: list[str] | None) -> list[WeekdaySelect] | None:
    """Parse service weekday strings."""
    if value is None:
        return None
    return [WeekdaySelect(weekday) for weekday in value]


async def _async_refresh_status(chihiros_data: ChihirosData) -> None:
    """Refresh schedule sensors after a schedule write."""
    try:
        await chihiros_data.coordinator.async_request_status()
    except Exception:
        _LOGGER.debug("Failed to refresh Chihiros status after schedule write", exc_info=True)
