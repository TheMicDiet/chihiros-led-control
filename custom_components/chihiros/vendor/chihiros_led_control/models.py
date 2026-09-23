"""Typed metadata describing Chihiros device families."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping, TypeAlias


class DeviceKind(StrEnum):
    """Supported device families."""

    LED = "led"
    DOSING_PUMP = "dosing_pump"
    MAG_STIRRER = "mag_stirrer"
    HEATER = "heater"


class LedProtocol(StrEnum):
    """LED command protocol variants."""

    BLE_LED = "BleLed"
    NEW_BLE_LED = "NewBleLed"
    SEA_LED = "SeaLed"


class LedFeature(StrEnum):
    """Optional LED capabilities."""

    FAN = "fan"
    TEMPERATURE_PROTECTION = "temperature_protection"
    INDICATOR_LED = "indicator_led"


@dataclass(frozen=True)
class LedSpec:
    """Protocol and capability metadata for an LED controller."""

    channels: Mapping[str, int]
    protocol: LedProtocol = LedProtocol.BLE_LED
    features: frozenset[LedFeature] = frozenset()
    min_fan_speed: int = 0


@dataclass(frozen=True)
class DosingPumpSpec:
    """Dosing-pump family metadata."""

    channel_limit: int = 8


@dataclass(frozen=True)
class MagStirrerSpec:
    """Magnetic-stirrer family metadata."""

    channel_limit: int = 8


@dataclass(frozen=True)
class HeaterSpec:
    """Heater family metadata."""


DeviceSpec: TypeAlias = LedSpec | DosingPumpSpec | MagStirrerSpec | HeaterSpec


@dataclass(frozen=True)
class DeviceModel:
    """Static product identity and discriminated device specification."""

    name: str
    advertised_codes: tuple[str, ...]
    spec: DeviceSpec
    needs_device_type: bool = False
    fallback: bool = False

    def __post_init__(self) -> None:
        """Reject untyped metadata at the profile boundary."""
        if not isinstance(self.spec, (LedSpec, DosingPumpSpec, MagStirrerSpec, HeaterSpec)):
            raise TypeError("spec must be a LedSpec, DosingPumpSpec, MagStirrerSpec, or HeaterSpec")

    @property
    def device_kind(self) -> DeviceKind:
        """Return the family discriminator derived from the specification."""
        if isinstance(self.spec, LedSpec):
            return DeviceKind.LED
        if isinstance(self.spec, DosingPumpSpec):
            return DeviceKind.DOSING_PUMP
        if isinstance(self.spec, MagStirrerSpec):
            return DeviceKind.MAG_STIRRER
        return DeviceKind.HEATER

    @property
    def color_channels(self) -> Mapping[str, int]:
        """Return LED channels, or an empty mapping for non-LED devices."""
        return self.spec.channels if isinstance(self.spec, LedSpec) else MappingProxyType({})

    @property
    def min_fan_speed(self) -> int:
        """Return the minimum supported fan speed."""
        return self.spec.min_fan_speed if isinstance(self.spec, LedSpec) else 0


WHITE_CHANNELS = MappingProxyType({"white": 0})
RGB_CHANNELS = MappingProxyType({"red": 0, "green": 1, "blue": 2})
WRGB_CHANNELS = MappingProxyType({"white": 3, "red": 0, "green": 1, "blue": 2})
COMMANDER_CHANNELS = MappingProxyType({"red": 0, "green": 1, "blue": 2, "white": 3})
X300_CHANNELS = MappingProxyType({"white": 0, "warm": 1})
TINY_TERRARIUM_EGG_CHANNELS = MappingProxyType({"red": 0, "green": 1})
Z_LIGHT_TINY_CHANNELS = MappingProxyType({"white": 0, "warm": 1})
