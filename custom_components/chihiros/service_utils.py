"""Shared helpers for Home Assistant services."""

from __future__ import annotations

import datetime
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .models import ChihirosData
from .vendor.chihiros_led_control.weekday_encoding import (
    WeekdaySelect,
    encode_selected_weekdays,
)

ATTR_ADDRESS = "address"
ATTR_ENTRY_ID = "entry_id"
ATTR_DEVICE_ID = "device_id"
ATTR_WEEKDAYS = "weekdays"
ATTR_FREQUENCY = "frequency"

WEEKDAY_VALUES = [weekday.value for weekday in WeekdaySelect]

DEVICE_SELECTOR_SCHEMA = {
    vol.Exclusive(ATTR_DEVICE_ID, "device"): vol.All(str, vol.Length(min=1)),
    vol.Exclusive(ATTR_ENTRY_ID, "device"): vol.All(str, vol.Length(min=1)),
    vol.Exclusive(ATTR_ADDRESS, "device"): vol.All(str, vol.Length(min=1)),
}


def _resolve_by_address(entries: dict[str, ChihirosData], address: str) -> ChihirosData:
    """Find one configured device by Bluetooth address (case-insensitive)."""
    normalized_address = address.upper()
    for chihiros_data in entries.values():
        if chihiros_data.device.address.upper() == normalized_address:
            return chihiros_data
    raise HomeAssistantError(f"Chihiros device address not found: {address}")


def _resolve_by_device_id(hass: HomeAssistant, device_id: str) -> ChihirosData:
    """Find one configured Chihiros device by Home Assistant device ID."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise HomeAssistantError(f"Chihiros device not found: {device_id}")
    entries: dict[str, ChihirosData] = hass.data.get(DOMAIN, {})
    for entry_id in device.config_entries:
        if entry_id in entries:
            return entries[entry_id]
    raise HomeAssistantError(f"Chihiros config entry not found for device {device.name}")


def _resolve_by_entry_id(entries: dict[str, ChihirosData], entry_id: str) -> ChihirosData:
    """Find one configured device by config entry ID."""
    if entry_id in entries:
        return entries[entry_id]
    raise HomeAssistantError(f"Chihiros config entry not found: {entry_id}")


def _resolve_sole_device(entries: dict[str, ChihirosData]) -> ChihirosData:
    """Return the only configured device, or raise when the target is ambiguous."""
    if len(entries) == 1:
        return next(iter(entries.values()))
    raise HomeAssistantError("Multiple Chihiros devices are configured; provide a device, entry_id, or address")


def resolve_service_device(hass: HomeAssistant, data: dict[str, Any]) -> ChihirosData:
    """Resolve a service call to one configured Chihiros device."""
    entries: dict[str, ChihirosData] = hass.data.get(DOMAIN, {})

    if device_id := data.get(ATTR_DEVICE_ID):
        return _resolve_by_device_id(hass, device_id)
    if entry_id := data.get(ATTR_ENTRY_ID):
        return _resolve_by_entry_id(entries, entry_id)
    if address := data.get(ATTR_ADDRESS):
        return _resolve_by_address(entries, address)
    return _resolve_sole_device(entries)


def parse_start_minutes(value: str | datetime.time) -> int:
    """Parse an ``HH:MM``/``HH:MM:SS`` string or time into minutes since midnight."""
    if isinstance(value, datetime.time):
        return value.hour * 60 + value.minute
    parts = str(value).strip().split(":")
    if len(parts) not in (2, 3):
        raise vol.Invalid(f"Invalid start time {value!r}, expected HH:MM")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as ex:
        raise vol.Invalid(f"Invalid start time {value!r}, expected HH:MM") from ex
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise vol.Invalid(f"Invalid start time {value!r}")
    return hour * 60 + minute


def frequency_from_service_data(data: dict[str, Any], *, default: int = 127) -> int:
    """Resolve the weekday repetition from service data as a bitmask.

    Accepts the user-friendly ``weekdays`` selector (a list of weekday
    names, ``everyday`` included) and falls back to the raw ``frequency``
    bitmask for advanced callers. Neither given means every day.
    """
    weekdays = data.get(ATTR_WEEKDAYS)
    if weekdays:
        return encode_selected_weekdays([WeekdaySelect(day) for day in weekdays])
    frequency = data.get(ATTR_FREQUENCY)
    if frequency is None:
        return default
    return int(frequency)
