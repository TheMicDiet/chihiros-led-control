"""Fake-device roster and family dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from ..vendor.chihiros_led_control.models import (
    RGB_CHANNELS,
    WHITE_CHANNELS,
    WRGB_CHANNELS,
    X300_CHANNELS,
    DeviceKind,
    DeviceModel,
    LedFeature,
    LedProtocol,
    LedSpec,
)
from ..vendor.chihiros_led_control.registry import DOSING_PUMP, HEATER, MAG_STIRRER
from .base import FakeBaseDevice
from .dosing import FakeDosingDevice
from .heater import FakeHeaterDevice
from .led import FakeLedDevice
from .stirrer import FakeStirrerDevice


@dataclass(frozen=True)
class FakeChihirosDeviceInfo:
    """Static fake device metadata."""

    address: str
    name: str
    model: DeviceModel


FAKE_ADDRESS_PREFIX = "FA:CE:C0"
FAKE_DEVICES_ENV = "CHIHIROS_FAKE_DEVICES"

FAKE_DEVICES = (
    FakeChihirosDeviceInfo(
        f"{FAKE_ADDRESS_PREFIX}:00:00:01",
        "DYNW60-fake",
        DeviceModel(
            "Fake WRGB II",
            ("DYNW60",),
            LedSpec(RGB_CHANNELS, protocol=LedProtocol.SEA_LED, features=frozenset(), min_fan_speed=0),
        ),
    ),
    FakeChihirosDeviceInfo(
        f"{FAKE_ADDRESS_PREFIX}:00:00:02",
        "DYWPRO60-fake",
        DeviceModel(
            "Fake WRGB II Pro",
            ("DYWPRO60",),
            LedSpec(WRGB_CHANNELS, protocol=LedProtocol.SEA_LED, features=frozenset(), min_fan_speed=0),
        ),
    ),
    FakeChihirosDeviceInfo(
        f"{FAKE_ADDRESS_PREFIX}:00:00:03",
        "DYNA2-fake",
        DeviceModel(
            "Fake A II",
            ("DYNA2",),
            LedSpec(WHITE_CHANNELS, protocol=LedProtocol.SEA_LED, features=frozenset(), min_fan_speed=0),
        ),
    ),
    FakeChihirosDeviceInfo(f"{FAKE_ADDRESS_PREFIX}:00:00:04", "DYDOSE-fake", DOSING_PUMP),
    FakeChihirosDeviceInfo(
        f"{FAKE_ADDRESS_PREFIX}:00:00:05",
        "DYVVD3-fake",
        DeviceModel(
            "Fake WRGB VIVID III",
            ("DYVVD3",),
            LedSpec(
                WRGB_CHANNELS,
                protocol=LedProtocol.SEA_LED,
                features=frozenset(
                    {
                        LedFeature.FAN,
                        LedFeature.TEMPERATURE_PROTECTION,
                        LedFeature.INDICATOR_LED,
                    }
                ),
                min_fan_speed=25,
            ),
        ),
    ),
    FakeChihirosDeviceInfo(
        f"{FAKE_ADDRESS_PREFIX}:00:00:06",
        "DYA-fake",
        DeviceModel(
            "Fake A Series",
            ("DYA",),
            LedSpec(WHITE_CHANNELS, protocol=LedProtocol.BLE_LED, features=frozenset(), min_fan_speed=0),
        ),
    ),
    FakeChihirosDeviceInfo(
        f"{FAKE_ADDRESS_PREFIX}:00:00:07",
        "DYC-fake",
        DeviceModel(
            "Fake New C",
            ("DYC",),
            LedSpec(WHITE_CHANNELS, protocol=LedProtocol.BLE_LED, features=frozenset(), min_fan_speed=0),
        ),
    ),
    FakeChihirosDeviceInfo(
        f"{FAKE_ADDRESS_PREFIX}:00:00:08",
        "DYARGB-fake",
        DeviceModel(
            "Fake RGB+APLUS",
            ("DYARGB",),
            LedSpec(RGB_CHANNELS, protocol=LedProtocol.BLE_LED, features=frozenset(), min_fan_speed=0),
        ),
    ),
    FakeChihirosDeviceInfo(
        f"{FAKE_ADDRESS_PREFIX}:00:00:09",
        "DYREE-fake",
        DeviceModel(
            "Fake RGB VIVID",
            ("DYREE",),
            LedSpec(RGB_CHANNELS, protocol=LedProtocol.BLE_LED, features=frozenset(), min_fan_speed=0),
        ),
    ),
    FakeChihirosDeviceInfo(
        f"{FAKE_ADDRESS_PREFIX}:00:00:0A",
        "DYRGBV-fake",
        DeviceModel(
            "Fake RGB VIVID II",
            ("DYRGBV",),
            LedSpec(RGB_CHANNELS, protocol=LedProtocol.NEW_BLE_LED, features=frozenset(), min_fan_speed=0),
        ),
    ),
    FakeChihirosDeviceInfo(
        f"{FAKE_ADDRESS_PREFIX}:00:00:0B",
        "DYSEA-fake",
        DeviceModel(
            "Fake SEA_LED",
            ("DYSEA",),
            LedSpec(WRGB_CHANNELS, protocol=LedProtocol.SEA_LED, features=frozenset(), min_fan_speed=0),
        ),
    ),
    FakeChihirosDeviceInfo(
        f"{FAKE_ADDRESS_PREFIX}:00:00:0C",
        "DYONE-fake",
        DeviceModel(
            "Fake Commander X",
            ("DYONE",),
            LedSpec(WHITE_CHANNELS, protocol=LedProtocol.BLE_LED, features=frozenset(), min_fan_speed=0),
        ),
    ),
    FakeChihirosDeviceInfo(
        f"{FAKE_ADDRESS_PREFIX}:00:00:0D",
        "DYTWO-fake",
        DeviceModel(
            "Fake X300",
            ("DYTWO",),
            LedSpec(X300_CHANNELS, protocol=LedProtocol.BLE_LED, features=frozenset(), min_fan_speed=0),
        ),
    ),
    FakeChihirosDeviceInfo(
        f"{FAKE_ADDRESS_PREFIX}:00:00:0E",
        "DYNLED-fake",
        DeviceModel(
            "Fake Commander 4",
            ("DYNLED",),
            LedSpec(WRGB_CHANNELS, protocol=LedProtocol.SEA_LED, features=frozenset(), min_fan_speed=0),
        ),
    ),
    FakeChihirosDeviceInfo(f"{FAKE_ADDRESS_PREFIX}:00:00:0F", "DYMIXR-fake", MAG_STIRRER),
    FakeChihirosDeviceInfo(f"{FAKE_ADDRESS_PREFIX}:00:00:10", "DYHET-fake", HEATER),
)
FAKE_DEVICES_BY_ADDRESS = {device.address: device for device in FAKE_DEVICES}

FakeDevice: TypeAlias = FakeBaseDevice | FakeLedDevice | FakeDosingDevice | FakeStirrerDevice | FakeHeaterDevice


def create_fake_device(address: str, pump_count: int = 4) -> FakeDevice:
    """Create a family-specific fake from a fake address."""
    info = FAKE_DEVICES_BY_ADDRESS[address]
    match info.model.device_kind:
        case DeviceKind.LED:
            return FakeLedDevice(info)
        case DeviceKind.DOSING_PUMP:
            return FakeDosingDevice(info, pump_count)
        case DeviceKind.MAG_STIRRER:
            return FakeStirrerDevice(info)
        case DeviceKind.HEATER:
            return FakeHeaterDevice(info)
    raise ValueError(f"Unsupported fake device kind: {info.model.device_kind}")
