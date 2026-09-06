"""Shared helpers for Home Assistant services."""

from __future__ import annotations

import datetime
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .models import ChihirosData

ATTR_ADDRESS = "address"
ATTR_ENTRY_ID = "entry_id"

DEVICE_SELECTOR_SCHEMA = {
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


def resolve_service_device(hass: HomeAssistant, data: dict[str, Any]) -> ChihirosData:
    """Resolve a service call to one configured Chihiros device."""
    entries: dict[str, ChihirosData] = hass.data.get(DOMAIN, {})
    if entry_id := data.get(ATTR_ENTRY_ID):
        if entry_id in entries:
            return entries[entry_id]
        raise HomeAssistantError(f"Chihiros config entry not found: {entry_id}")

    if address := data.get(ATTR_ADDRESS):
        return _resolve_by_address(entries, address)

    if len(entries) == 1:
        return next(iter(entries.values()))
    raise HomeAssistantError("Multiple Chihiros devices are configured; provide entry_id or address")


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
