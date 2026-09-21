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


@dataclass(frozen=True, init=False)
class DeviceModel:
    """Static product identity and discriminated device specification.

    ``color_channels`` and the derived capability properties remain read-only
    views for callers that consumed the pre-profile metadata API. New code
    should use ``spec`` and ``device_kind``.
    """

    name: str
    advertised_codes: tuple[str, ...]
    spec: DeviceSpec
    needs_device_type: bool = False
    fallback: bool = False

    def __init__(
        self,
        name: str,
        advertised_codes: tuple[str, ...],
        spec_or_channels: DeviceSpec | Mapping[str, int],
        needs_device_type: bool = False,
        fallback: bool = False,
        *,
        has_fan: bool = False,
        min_fan_speed: int = 0,
        is_vivid3: bool = False,
        sea_led_family: bool = False,
        is_heater: bool = False,
    ) -> None:
        """Create metadata, accepting legacy keyword spelling during migration."""
        if isinstance(spec_or_channels, (LedSpec, DosingPumpSpec, MagStirrerSpec, HeaterSpec)):
            spec = spec_or_channels
        elif is_heater:
            spec = HeaterSpec()
        elif name == "Dosing Pump":
            spec = DosingPumpSpec()
        elif name == "Mag Stirrer":
            spec = MagStirrerSpec()
        else:
            features = frozenset(
                feature
                for feature, enabled in (
                    (LedFeature.FAN, has_fan),
                    (LedFeature.TEMPERATURE_PROTECTION, is_vivid3),
                    (LedFeature.INDICATOR_LED, is_vivid3),
                )
                if enabled
            )
            protocol = LedProtocol.SEA_LED if sea_led_family else LedProtocol.BLE_LED
            spec = LedSpec(spec_or_channels, protocol, features, min_fan_speed)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "advertised_codes", advertised_codes)
        object.__setattr__(self, "spec", spec)
        object.__setattr__(self, "needs_device_type", needs_device_type)
        object.__setattr__(self, "fallback", fallback)

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
    def has_fan(self) -> bool:
        """Return whether this LED exposes fan controls."""
        return isinstance(self.spec, LedSpec) and LedFeature.FAN in self.spec.features

    @property
    def min_fan_speed(self) -> int:
        """Return the minimum supported fan speed."""
        return self.spec.min_fan_speed if isinstance(self.spec, LedSpec) else 0

    @property
    def is_vivid3(self) -> bool:
        """Return whether this profile exposes VIVID III-only controls."""
        return isinstance(self.spec, LedSpec) and LedFeature.TEMPERATURE_PROTECTION in self.spec.features

    @property
    def sea_led_family(self) -> bool:
        """Return whether LED auto points use SeaLed encoding."""
        return isinstance(self.spec, LedSpec) and self.spec.protocol is LedProtocol.SEA_LED

    @property
    def is_heater(self) -> bool:
        """Return whether this profile is a heater."""
        return isinstance(self.spec, HeaterSpec)


def __getattr__(name: str):
    """Lazily expose registry declarations during the package cutover."""
    if name in {
        "DOSING_PUMP",
        "FALLBACK",
        "GENERIC_MODELS_BY_DEVICE_TYPE",
        "GENERIC_RGB",
        "GENERIC_WHITE",
        "GENERIC_WRGB",
        "HEATER",
        "MAG_STIRRER",
        "MODEL_BY_CODE",
        "SUPPORTED_MODELS",
        "iter_model_codes_by_specificity",
    }:
        from . import registry

        return getattr(registry, name)
    raise AttributeError(name)


WHITE_CHANNELS = MappingProxyType({"white": 0})
RGB_CHANNELS = MappingProxyType({"red": 0, "green": 1, "blue": 2})
WRGB_CHANNELS = MappingProxyType({"white": 3, "red": 0, "green": 1, "blue": 2})
COMMANDER_CHANNELS = MappingProxyType({"red": 0, "green": 1, "blue": 2, "white": 3})
X300_CHANNELS = MappingProxyType({"white": 0, "warm": 1})
TINY_TERRARIUM_EGG_CHANNELS = MappingProxyType({"red": 0, "green": 1})
Z_LIGHT_TINY_CHANNELS = MappingProxyType({"white": 0, "warm": 1})
