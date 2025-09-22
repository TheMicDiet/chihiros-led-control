"""Chihiros HA integration root module."""

from __future__ import annotations

import logging

try:
    from homeassistant.components import bluetooth
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.const import Platform
    from homeassistant.core import HomeAssistant
    from homeassistant.exceptions import ConfigEntryNotReady

    PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.SWITCH, Platform.BUTTON, Platform.NUMBER]
except ModuleNotFoundError:
    pass

from .chihiros_led_control.device import BaseDevice, get_model_class_from_name
from .const import DOMAIN
from .coordinator import ChihirosDataUpdateCoordinator
from .models import ChihirosData
from .chihiros_doser_control import register_services as register_doser_services

_LOGGER = logging.getLogger(__name__)


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
    model_class = get_model_class_from_name(ble_device.name)
    # TODO add password support
    chihiros_device: BaseDevice = model_class(ble_device)

    coordinator = ChihirosDataUpdateCoordinator(
        hass,
        chihiros_device,
        ble_device,
    )

    # Heuristic: classify doser by name; keep some handy attrs
    is_doser = any(k in ble_device.name.lower() for k in ("doser", "dose", "dydose"))
    coordinator.device_type = "doser" if is_doser else "led"
    coordinator.address = address
    coordinator.name = entry.title

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = ChihirosData(
        entry.title, chihiros_device, coordinator
    )

    # Register the manual-dose service once (idempotent in the submodule)
    await register_doser_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
