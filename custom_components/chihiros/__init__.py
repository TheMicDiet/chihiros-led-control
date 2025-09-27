"""Chihiros HA integration root module."""

from __future__ import annotations

import logging

try:
    from homeassistant.components import bluetooth
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.const import Platform
    from homeassistant.core import HomeAssistant
    from homeassistant.exceptions import ConfigEntryNotReady

    PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.SWITCH]
except ModuleNotFoundError:
    pass

from homeassistant.const import CONF_NAME

from .chihiros_led_control.device import BaseDevice, get_model_class_from_name
from .chihiros_led_control.device.commander1 import Commander1
from .chihiros_led_control.device.commander4 import Commander4
from .chihiros_led_control.device.fallback import Fallback
from .chihiros_led_control.device.generic_rgb import GenericRGB
from .chihiros_led_control.device.generic_white import GenericWhite
from .chihiros_led_control.device.generic_wrgb import GenericWRGB
from .const import DOMAIN
from .coordinator import ChihirosDataUpdateCoordinator
from .models import ChihirosData

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

    chihiros_device: BaseDevice = model_class(ble_device)

    # If fallback or commander device, allow user-provided name and device_type (white/rgb/wrgb)
    if isinstance(chihiros_device, (Fallback, Commander1, Commander4)):
        # Apply name override if provided
        entry_name = entry.data.get(CONF_NAME)
        if entry_name:
            # Some BLEDevice implementations allow setting name attribute
            try:
                ble_device.name = entry_name
            except Exception:
                pass
        # Configure colors based on device_type
        device_type = entry.data.get("device_type")
        if device_type == "rgb":
            chihiros_device = GenericRGB(ble_device)
        elif device_type == "wrgb":
            chihiros_device = GenericWRGB(ble_device)
        else:
            chihiros_device = GenericWhite(ble_device)

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
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
