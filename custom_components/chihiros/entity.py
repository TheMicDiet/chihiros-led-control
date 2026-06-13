"""Shared Home Assistant entity helpers for Chihiros."""

from __future__ import annotations

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import MANUFACTURER
from .vendor.chihiros_led_control import ChihirosDevice


def chihiros_device_info(device: ChihirosDevice, address: str) -> DeviceInfo:
    """Return Home Assistant device metadata for a Chihiros device."""
    return DeviceInfo(
        connections={(dr.CONNECTION_BLUETOOTH, address)},
        manufacturer=MANUFACTURER,
        model=device.model_name,
        name=device.name,
    )


def chihiros_entity_name(device: ChihirosDevice, suffix: str) -> str:
    """Return a consistent Chihiros entity name."""
    return f"{device.name} {suffix}"


def chihiros_unique_id(address: str, suffix: str) -> str:
    """Return a consistent Chihiros unique id."""
    return f"{address}_{suffix}"
