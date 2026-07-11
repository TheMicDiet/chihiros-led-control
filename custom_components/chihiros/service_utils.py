"""Shared helpers for Home Assistant services."""

from __future__ import annotations

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


def resolve_service_device(hass: HomeAssistant, data: dict[str, Any]) -> ChihirosData:
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
