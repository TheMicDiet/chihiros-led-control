"""Chihiros HA integration root module."""

from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN
from .coordinator import ChihirosDataUpdateCoordinator
from .models import ChihirosData
from .vendor.chihiros_led_control import (
    ChihirosDevice,
    create_device,
    needs_device_type,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up chihiros from a config entry."""
    if entry.unique_id is None:
        raise ConfigEntryNotReady(f"Entry doesn't have any unique_id {entry.title}")
    address: str = entry.unique_id
    ble_device = bluetooth.async_ble_device_from_address(hass, address.upper(), True)
    if not ble_device:
        raise ConfigEntryNotReady(
            f"Could not find Chihiros BLE device with address {address}"
        )
    if not ble_device.name:
        raise ConfigEntryNotReady(
            f"Found Chihiros BLE device with address {address} but can not find its name"
        )
    if needs_device_type(ble_device.name):
        entry_name = entry.data.get(CONF_NAME)
        if entry_name:
            try:
                ble_device.name = entry_name
            except Exception:
                pass

    chihiros_device: ChihirosDevice = create_device(
        ble_device, device_type=entry.data.get("device_type")
    )

    coordinator = ChihirosDataUpdateCoordinator(
        hass,
        chihiros_device,
        ble_device,
    )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = ChihirosData(
        entry.title, chihiros_device, coordinator
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        chihiros_data: ChihirosData = hass.data[DOMAIN].pop(entry.entry_id)
        await chihiros_data.device.disconnect()

    return unload_ok
