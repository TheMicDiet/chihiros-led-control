"""Tests for Chihiros device model detection and factory helpers."""

from __future__ import annotations

import asyncio

from chihiros_led_control.client import ChihirosDosingPump
from chihiros_led_control.factory import (
    create_device,
    detect_model,
    needs_device_type,
    resolve_model,
)
from chihiros_led_control.models import FALLBACK


class FakeBLEDevice:
    """Small BLEDevice stand-in for factory tests."""

    def __init__(self, name: str | None = None) -> None:
        """Create a fake BLE device."""
        self.name = name
        self.address = "AA:BB:CC:DD:EE:FF"


def test_detect_model_matches_name_prefix() -> None:
    """Model detection matches advertised name prefixes."""
    assert detect_model("DYNW601234567890").name == "WRGB II"


def test_detect_model_matches_legacy_wrgb_prefix() -> None:
    """Model detection matches the legacy WRGB prefix from app templates."""
    assert detect_model("DYWRGB1234567890").name == "WRGB II"


def test_detect_model_matches_esphome_wrgb_prefix() -> None:
    """Model detection matches the WRGB prefix observed in the ESPHome bridge."""
    assert detect_model("DYNT901234567890").name == "WRGB II"


def test_detect_model_does_not_rely_on_fixed_slicing() -> None:
    """Model detection works without fixed suffix slicing."""
    assert detect_model("DYSL120-short").name == "WRGB II Slim"


def test_detect_model_matches_dosing_pump_prefix() -> None:
    """Model detection matches dosing pump advertisements."""
    assert detect_model("DYDOSE1234567890").name == "Dosing Pump"


def test_unknown_model_needs_device_type() -> None:
    """Unknown models use fallback metadata and need a type."""
    assert detect_model("UNKNOWN").fallback is True
    assert needs_device_type("UNKNOWN") is True


def test_commander_model_needs_device_type() -> None:
    """Commander devices need a user-selected generic type."""
    assert needs_device_type("DYCOM123456789") is True


def test_resolve_fallback_device_type() -> None:
    """Fallback models resolve to a generic device type."""
    model = resolve_model("UNKNOWN", FALLBACK, "rgb")

    assert model.name == "Generic RGB"
    assert dict(model.color_channels) == {"red": 0, "green": 1, "blue": 2}


def test_factory_created_device_uses_generic_wrgb_model() -> None:
    """Factory-created devices expose generic WRGB metadata."""

    async def create() -> tuple[str, dict[str, int]]:
        device = create_device(FakeBLEDevice("UNKNOWN"), device_type="wrgb")  # type: ignore[arg-type]
        return device.model_name, device.colors

    model_name, colors = asyncio.run(create())

    assert model_name == "Generic WRGB"
    assert colors == {"white": 3, "red": 0, "green": 1, "blue": 2}


def test_factory_created_dosing_pump_uses_dosing_client() -> None:
    """Factory-created dosing pump devices use the dosing client class."""

    async def create() -> ChihirosDosingPump:
        return create_device(FakeBLEDevice("DYDOSE1234567890"))  # type: ignore[arg-type, return-value]

    device = asyncio.run(create())

    assert isinstance(device, ChihirosDosingPump)
    assert device.model_name == "Dosing Pump"
    assert device.colors == {}
