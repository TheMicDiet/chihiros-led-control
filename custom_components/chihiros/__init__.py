"""Chihiros HA integration root module."""

from __future__ import annotations

import logging

try:
    from homeassistant.components import bluetooth
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.const import Platform
    from homeassistant.core import HomeAssistant
    from homeassistant.exceptions import ConfigEntryNotReady

    # Keep the full list here; we’ll choose dynamically at runtime
    PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.SWITCH, Platform.BUTTON, Platform.NUMBER]
except ModuleNotFoundError:
    # Allows static analysis outside HA
    pass

from .chihiros_led_control.device import BaseDevice, get_model_class_from_name
from .const import DOMAIN
from .coordinator import ChihirosDataUpdateCoordinator
from .models import ChihirosData
from .chihiros_doser_control import register_services as register_doser_services

_LOGGER = logging.getLogger(__name__)


def _guess_channel_count(name: str | None) -> int:
    """Best-effort channel count from BLE name."""
    s = (name or "").lower()
    # Common patterns: DYDOSED2..., 2CH, etc.
    if "d1" in s or "1ch" in s or "1-channel" in s:
        return 1
    if "d2" in s or "2ch" in s or "2-channel" in s:
        return 2
    if "d3" in s or "3ch" in s or "3-channel" in s:
        return 3
    if "d4" in s or "4ch" in s or "4-channel" in s:
        return 4
    return 4  # sensible default


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up chihiros from a config entry."""
    if entry.unique_id is None:
        raise ConfigEntryNotReady(f"Entry doesn't have any unique_id {entry.title}")

    address: str = entry.unique_id
    ble_device = bluetooth.async_ble_device_from_address(hass, address.upper(), True)
    if not ble_device:
        raise ConfigEntryNotReady(f"Could not find Chihiros BLE device with address {address}")
    if not ble_device.name:
        raise ConfigEntryNotReady(
            f"Found Chihiros BLE device with address {address} but can not find its name"
        )

    model_class = get_model_class_from_name(ble_device.name)
    # TODO: add password support
    chihiros_device: BaseDevice = model_class(ble_device)

    coordinator = ChihirosDataUpdateCoordinator(
        hass,
        chihiros_device,
        ble_device,
    )

    # Classify device type and stash handy attrs for platforms
    is_doser = any(k in ble_device.name.lower() for k in ("doser", "dose", "dydose"))
    coordinator.device_type = "doser" if is_doser else "led"
    coordinator.address = address
    # Options → explicit enabled channels (subset of 1..4). Fallback to heuristic.
    opt_enabled = entry.options.get("enabled_channels")
    if opt_enabled:
        try:
            enabled = sorted({int(x) for x in opt_enabled if 1 <= int(x) <= 4})
        except Exception:
            enabled = [1, 2, 3, 4]
        if not enabled:
            enabled = [1]
        coordinator.enabled_channels = enabled
        coordinator.channel_count = len(enabled)
    else:
        guessed = _guess_channel_count(ble_device.name)
        coordinator.enabled_channels = list(range(1, guessed + 1))
        coordinator.channel_count = guessed

    # Choose platforms per device type
    platforms_to_load: list[Platform] = (
        [Platform.BUTTON, Platform.NUMBER] if is_doser else [Platform.LIGHT, Platform.SWITCH]
    )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = ChihirosData(entry.title, chihiros_device, coordinator)

    # Register manual-dose service (idempotent in submodule)
    await register_doser_services(hass)

    # Reload entry when options change (e.g., channels updated)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Only load matching platforms
    await hass.config_entries.async_forward_entry_setups(entry, platforms_to_load)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data: ChihirosData | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)  # type: ignore[assignment]
    if data and getattr(data.coordinator, "device_type", "led") == "doser":
        platforms_to_unload = [Platform.BUTTON, Platform.NUMBER]
    else:
        platforms_to_unload = [Platform.LIGHT, Platform.SWITCH]

    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms_to_unload)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options updates by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)