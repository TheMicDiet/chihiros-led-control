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
    assert detect_model("DYNDOSCDA1ECD07A4D").name == "Dosing Pump"


def test_detect_model_matches_wrgb_vivid_iii_prefix() -> None:
    """Model detection matches WRGB VIVID III advertisements and enables fan support."""
    model = detect_model("DYVVD3CDA1ECD07A4D")

    assert model.name == "WRGB VIVID III"
    assert dict(model.color_channels) == {"white": 3, "red": 0, "green": 1, "blue": 2}
    assert model.has_fan is True


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


def test_detect_model_matches_rgb_aplus_prefixes() -> None:
    """RGB+APLUS advertisements (old and new generation) resolve to a 3-channel RGB model."""
    for name in ("DYARGB1234567890", "DYRGBA+1234567890", "DYRGBA1234567890", "DYNARGB1234567890"):
        model = detect_model(name)

        assert model.name == "RGB+APLUS"
        assert dict(model.color_channels) == {"red": 0, "green": 1, "blue": 2}


def test_detect_model_matches_rgb_vivid_prefixes() -> None:
    """RGB VIVID and RGB VIVID II advertisements resolve to RGB models."""
    assert detect_model("DYREE1234567890").name == "RGB VIVID"
    for name in ("DYRGBV1234567890", "DYNVVD1234567890", "DYNV1234567890"):
        model = detect_model(name)

        assert model.name == "RGB VIVID II"
        assert dict(model.color_channels) == {"red": 0, "green": 1, "blue": 2}


def test_detect_model_matches_single_channel_white_prefixes() -> None:
    """A series, New C, and Commander X are single-channel white devices."""
    assert dict(detect_model("DYA1234567890").color_channels) == {"white": 0}
    assert detect_model("DYA1234567890").name == "A Series"
    assert detect_model("DYC1234567890").name == "New C"
    assert dict(detect_model("DYNC2CDA1ECD07A4D").color_channels) == {"white": 0}
    assert detect_model("DYONE1234567890").name == "Commander X"


def test_detect_model_matches_x300_two_channel_prefix() -> None:
    """X300 is a two-channel white plus warm device."""
    model = detect_model("DYTWO1234567890")

    assert model.name == "X300"
    assert dict(model.color_channels) == {"white": 0, "warm": 1}


def test_detect_model_matches_sea_led_prefix() -> None:
    """SEA_LED is a four-channel WRGB device."""
    model = detect_model("DYSEA1234567890")

    assert model.name == "SEA_LED"
    assert dict(model.color_channels) == {"white": 3, "red": 0, "green": 1, "blue": 2}


def test_detect_model_matches_new_gen_commander_prefix() -> None:
    """New-generation Commander 4 controllers need a user-selected generic type."""
    assert detect_model("DYNLED1234567890").name == "Commander 4"
    assert needs_device_type("DYNLED1234567890") is True
