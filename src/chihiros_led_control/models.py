"""Device model registry for Chihiros LEDs."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class DeviceModel:
    """Static metadata for a Chihiros LED model."""

    name: str
    advertised_codes: tuple[str, ...]
    color_channels: Mapping[str, int]
    needs_device_type: bool = False
    fallback: bool = False


WHITE_CHANNELS = MappingProxyType({"white": 0})
RGB_CHANNELS = MappingProxyType({"red": 0, "green": 1, "blue": 2})
WRGB_CHANNELS = MappingProxyType({"white": 3, "red": 0, "green": 1, "blue": 2})
COMMANDER_CHANNELS = MappingProxyType({"white": 0, "red": 0, "green": 1, "blue": 2})
DOSING_CHANNELS = MappingProxyType({})
TINY_TERRARIUM_EGG_CHANNELS = MappingProxyType({"red": 0, "green": 1})
Z_LIGHT_TINY_CHANNELS = MappingProxyType({"white": 0, "warm": 1})

GENERIC_WHITE = DeviceModel("Generic White LED", (), WHITE_CHANNELS)
GENERIC_RGB = DeviceModel("Generic RGB", (), RGB_CHANNELS)
GENERIC_WRGB = DeviceModel("Generic WRGB", (), WRGB_CHANNELS)
FALLBACK = DeviceModel("fallback", (), COMMANDER_CHANNELS, needs_device_type=True, fallback=True)
DOSING_PUMP = DeviceModel("Dosing Pump", ("DYDOSE", "DYDOSED", "DOSER"), DOSING_CHANNELS)

SUPPORTED_MODELS: tuple[DeviceModel, ...] = (
    DeviceModel("Z Light TINY", ("DYSSD", "DYZSD"), Z_LIGHT_TINY_CHANNELS),
    DeviceModel("Tiny Terrarium Egg", ("DYDD",), TINY_TERRARIUM_EGG_CHANNELS),
    DeviceModel("A II", ("DYNA2", "DYNA2N"), WHITE_CHANNELS),
    DeviceModel(
        "WRGB II",
        ("DYNT90", "DYWRGB", "DYNWRGB", "DYNW30", "DYNW45", "DYNW60", "DYNW90", "DYNW12P"),
        RGB_CHANNELS,
    ),
    DeviceModel(
        "WRGB II Pro",
        ("DYWPRO30", "DYWPRO45", "DYWPRO60", "DYWPRO80", "DYWPRO90", "DYWPR120"),
        WRGB_CHANNELS,
    ),
    DeviceModel(
        "WRGB II Slim",
        ("DYSILN", "DYSL30", "DYSL45", "DYSL60", "DYSL90", "DYSL120", "DYSL12"),
        RGB_CHANNELS,
    ),
    DeviceModel("C II", ("DYNC2N",), WHITE_CHANNELS),
    DeviceModel("C II RGB", ("DYNCRGP", "DYNCRGB"), RGB_CHANNELS),
    DeviceModel(
        "Universal WRGB",
        (
            "DYU550",
            "DYU600",
            "DYU700",
            "DYU800",
            "DYU920",
            "DYU1000",
            "DYU1200",
            "DYU1500",
        ),
        WRGB_CHANNELS,
    ),
    DeviceModel("Commander 1", ("DYCOM",), COMMANDER_CHANNELS, needs_device_type=True),
    DeviceModel("Commander 4", ("DYLED",), COMMANDER_CHANNELS, needs_device_type=True),
    DOSING_PUMP,
)

GENERIC_MODELS_BY_DEVICE_TYPE = MappingProxyType(
    {
        "white": GENERIC_WHITE,
        "rgb": GENERIC_RGB,
        "wrgb": GENERIC_WRGB,
    }
)

MODEL_BY_CODE = MappingProxyType({code: model for model in SUPPORTED_MODELS for code in model.advertised_codes})


def iter_model_codes_by_specificity() -> tuple[tuple[str, DeviceModel], ...]:
    """Return model codes sorted so longer prefixes win."""
    return tuple(
        sorted(
            MODEL_BY_CODE.items(),
            key=lambda code_model: len(code_model[0]),
            reverse=True,
        )
    )
