"""Typed metadata describing Chihiros device families."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
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


class LedLevelScale(StrEnum):
    """Verified model setter behavior, independent of the point time protocol."""

    RAW = "raw"
    NEW_A_SERIES = "NewASeriesLed"
    MAGNETIC = "MagneticLed"


class LedControllerMode(StrEnum):
    """Distinct LED schedule controller modes, independent of manual/auto state."""

    CUSTOM_CURVE = "custom_curve"
    SCENE = "scene"


NO_WEEKDAY_PREFIXES = (
    "DYZSD",
    "DYNA2N",
    "DYNA2",
    "DYNC2N",
    "DYCXD",
    "DYECO",
    "MDTM",
    "DYDD",
    "DYCOM",
    "DYNCM",
    "DYFISHDL",
    "DYLITE",
    "DYZLPD",
    "DYZPRO",
)


def supports_weekdays(device_name: str | None) -> bool:
    """Return the verified weekday-selector capability, without inferring a layout."""
    return not bool(device_name and device_name.startswith(NO_WEEKDAY_PREFIXES))


def replay_auto_on_reconnect(device_name: str | None) -> bool:
    """Return whether Lite auto initialization requires authoritative replay."""
    return bool(device_name and device_name.startswith(NO_WEEKDAY_PREFIXES))


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
    channel_maxima: Mapping[int, int] = field(default_factory=lambda: MappingProxyType({}))
    level_scale: LedLevelScale = LedLevelScale.RAW
    scene_min_firmware: int | None = 20

    def __post_init__(self) -> None:
        """Freeze level metadata and reject maxima that cannot fit in a level byte."""
        maxima = dict(self.channel_maxima)
        if any(channel not in self.channels.values() for channel in maxima):
            raise ValueError("Level maxima must refer to supported channel ids")
        if any(not isinstance(maximum, int) or not 1 <= maximum <= 254 for maximum in maxima.values()):
            raise ValueError("Channel maxima must be between 1 and 254 (255 means inactive)")
        object.__setattr__(self, "channel_maxima", MappingProxyType(maxima))

    def max_level(self, channel: int) -> int:
        """Return the raw model-unit maximum, defaulting to 100."""
        return self.channel_maxima.get(channel, 100)

    def validate_level(self, channel: int, level: int) -> None:
        """Reject raw inputs outside the selected channel's model range."""
        maximum = self.max_level(channel)
        if not isinstance(level, int) or not 0 <= level <= maximum:
            raise ValueError(f"Brightness level for channel {channel} must be between 0 and {maximum}")

    def to_wire_level(self, channel: int, level: int) -> int:
        """Encode raw model units; scaled setters preserve the special 0/1 values."""
        self.validate_level(channel, level)
        if self.level_scale is LedLevelScale.RAW or level <= 1:
            return level
        return level * 100 // self.max_level(channel)

    def from_wire_level(self, channel: int, level: int) -> int:
        """Decode valid wire levels to model units (quantization is not reversible)."""
        if self.level_scale is LedLevelScale.RAW:
            self.validate_level(channel, level)
            return level
        if not 0 <= level <= 100:
            raise ValueError("Scaled wire level must be between 0 and 100")
        return level if level <= 1 else level * self.max_level(channel) // 100

    def percent_to_level(self, channel: int, percent: float) -> int:
        """Convert a percentage to raw model units, rounding nonzero light upwards."""
        if not 0 <= percent <= 100:
            raise ValueError("Brightness percentage must be between 0 and 100")
        return math.ceil(percent * self.max_level(channel) / 100)


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

    @property
    def supports_weekdays(self) -> bool:
        """Default to weekday support unless all identities are classified Lite."""
        return not self.advertised_codes or any(supports_weekdays(code) for code in self.advertised_codes)

    @property
    def replay_auto_on_reconnect(self) -> bool:
        """Require replay by default only when all model identities are Lite."""
        return bool(self.advertised_codes) and all(replay_auto_on_reconnect(code) for code in self.advertised_codes)


WHITE_CHANNELS = MappingProxyType({"white": 0})
RGB_CHANNELS = MappingProxyType({"red": 0, "green": 1, "blue": 2})
WRGB_CHANNELS = MappingProxyType({"white": 3, "red": 0, "green": 1, "blue": 2})
COMMANDER_CHANNELS = MappingProxyType({"red": 0, "green": 1, "blue": 2, "white": 3})
X300_CHANNELS = MappingProxyType({"white": 0, "warm": 1})
TINY_TERRARIUM_EGG_CHANNELS = MappingProxyType({"red": 0, "green": 1})
Z_LIGHT_TINY_CHANNELS = MappingProxyType({"white": 0, "warm": 1})
