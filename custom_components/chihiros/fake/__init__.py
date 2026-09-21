"""Development-only family-specific fake Chihiros devices."""

from __future__ import annotations

import os
from collections.abc import Iterable

from .base import FakeBaseDevice
from .dosing import FakeDosingDevice
from .heater import FakeHeaterDevice
from .led import FakeLedDevice
from .registry import (
    FAKE_ADDRESS_PREFIX,
    FAKE_DEVICES,
    FAKE_DEVICES_BY_ADDRESS,
    FAKE_DEVICES_ENV,
    FakeChihirosDeviceInfo,
    FakeDevice,
    create_fake_device,
)
from .stirrer import FakeStirrerDevice


def fake_devices_enabled() -> bool:
    """Return whether fake devices are enabled for local development."""
    return os.environ.get(FAKE_DEVICES_ENV, "").lower() in {"1", "true", "yes", "on"}


def is_fake_address(address: str) -> bool:
    """Return whether an address belongs to a configured fake device."""
    return address in FAKE_DEVICES_BY_ADDRESS


def iter_enabled_fake_devices(current_addresses: Iterable[str]) -> tuple[FakeChihirosDeviceInfo, ...]:
    """Return fake devices that can be shown in discovery."""
    if not fake_devices_enabled():
        return ()
    configured_addresses = set(current_addresses)
    return tuple(device for device in FAKE_DEVICES if device.address not in configured_addresses)


__all__ = [
    "FAKE_ADDRESS_PREFIX",
    "FAKE_DEVICES",
    "FAKE_DEVICES_BY_ADDRESS",
    "FakeBaseDevice",
    "FakeChihirosDeviceInfo",
    "FakeDevice",
    "FakeDosingDevice",
    "FakeHeaterDevice",
    "FakeLedDevice",
    "FakeStirrerDevice",
    "create_fake_device",
    "fake_devices_enabled",
    "is_fake_address",
    "iter_enabled_fake_devices",
]
