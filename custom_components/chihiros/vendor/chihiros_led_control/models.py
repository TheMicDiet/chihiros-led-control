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
    has_fan: bool = False
    min_fan_speed: int = 0
    # SeaLed devices encode 0x5A/0x06 auto-curve points as [channel, hour, minute, level];
    # BleLed/NewBleLed devices use [channel, 30-min-slot, level] instead.
    sea_led_family: bool = False


WHITE_CHANNELS = MappingProxyType({"white": 0})
RGB_CHANNELS = MappingProxyType({"red": 0, "green": 1, "blue": 2})
WRGB_CHANNELS = MappingProxyType({"white": 3, "red": 0, "green": 1, "blue": 2})
# Commander family (incl. fallback default): channels red/green/blue/white on
# 0..3 per the 2.8.59 app registry's initColorNameList.
COMMANDER_CHANNELS = MappingProxyType({"red": 0, "green": 1, "blue": 2, "white": 3})
X300_CHANNELS = MappingProxyType({"white": 0, "warm": 1})
DOSING_CHANNELS = MappingProxyType({})
TINY_TERRARIUM_EGG_CHANNELS = MappingProxyType({"red": 0, "green": 1})
Z_LIGHT_TINY_CHANNELS = MappingProxyType({"white": 0, "warm": 1})

GENERIC_WHITE = DeviceModel("Generic White LED", (), WHITE_CHANNELS)
GENERIC_RGB = DeviceModel("Generic RGB", (), RGB_CHANNELS)
GENERIC_WRGB = DeviceModel("Generic WRGB", (), WRGB_CHANNELS)
FALLBACK = DeviceModel("fallback", (), COMMANDER_CHANNELS, needs_device_type=True, fallback=True)
DOSING_PUMP = DeviceModel("Dosing Pump", ("DYDOSE", "DYDOSED", "DYTDOS", "DYNDOS"), DOSING_CHANNELS)

SUPPORTED_MODELS: tuple[DeviceModel, ...] = (
    DeviceModel("Z Light TINY", ("DYSSD", "DYZSD"), Z_LIGHT_TINY_CHANNELS),
    DeviceModel("Tiny Terrarium Egg", ("DYDD",), TINY_TERRARIUM_EGG_CHANNELS),
    # A II (DYNA2/DYNA2N) is SeaLed per the 2.8.59 registry.
    DeviceModel("A II", ("DYNA2", "DYNA2N"), WHITE_CHANNELS, sea_led_family=True),
    DeviceModel("A Series", ("DYA",), WHITE_CHANNELS),
    # New C splits by generation: DYC is BleLed, DYNC2 is SeaLed (2.8.59 registry).
    DeviceModel("New C", ("DYC",), WHITE_CHANNELS),
    DeviceModel("New C", ("DYNC2",), WHITE_CHANNELS, sea_led_family=True),
    # RGB+APLUS splits by generation: DYARGB/DYRGBA+/DYRGBA are BleLed,
    # DYNARGB is SeaLed (2.8.59 registry).
    DeviceModel("RGB+APLUS", ("DYARGB", "DYRGBA+", "DYRGBA"), RGB_CHANNELS),
    DeviceModel("RGB+APLUS", ("DYNARGB",), RGB_CHANNELS, sea_led_family=True),
    DeviceModel("RGB VIVID", ("DYREE",), RGB_CHANNELS),
    # RGB VIVID II splits by generation: DYRGBV is NewBleLed, DYNVVD/DYNV are
    # SeaLed device_type (2.8.59 registry "RGB VIVID2").
    DeviceModel(
        "RGB VIVID II",
        ("DYRGBV",),
        RGB_CHANNELS,
    ),
    DeviceModel(
        "RGB VIVID II",
        ("DYNVVD", "DYNV"),
        RGB_CHANNELS,
        sea_led_family=True,
    ),
    DeviceModel(
        "SEA_LED",
        ("DYSEA",),
        WRGB_CHANNELS,
        sea_led_family=True,
    ),
    DeviceModel("Commander X", ("DYONE",), WHITE_CHANNELS),
    DeviceModel("X300", ("DYTWO",), X300_CHANNELS),
    # WRGB II: legacy DYWRGB is BleLed; the DYN-prefixed new generation is SeaLed.
    DeviceModel(
        "WRGB II",
        ("DYWRGB",),
        RGB_CHANNELS,
    ),
    DeviceModel(
        "WRGB II",
        ("DYNT90", "DYNW30", "DYNW45", "DYNW60", "DYNW90", "DYNW12P", "DYNWRGB"),
        RGB_CHANNELS,
        sea_led_family=True,
    ),
    DeviceModel(
        "WRGB II Pro",
        ("DYWPRO30", "DYWPRO45", "DYWPRO60", "DYWPRO80", "DYWPRO90", "DYWPR120"),
        WRGB_CHANNELS,
        # 30..120 are light lengths; SeaLed per the new-gen convention.
        sea_led_family=True,
    ),
    DeviceModel(
        "WRGB II Slim",
        ("DYSILN", "DYSL30", "DYSL45", "DYSL60", "DYSL90", "DYSL120", "DYSL12"),
        RGB_CHANNELS,
        # 30..120 are light lengths; SeaLed per the new-gen convention.
        sea_led_family=True,
    ),
    # NewVivid3 device_type is not in {BleLed, NewBleLed}, so the app treats it as SeaLed.
    DeviceModel(
        "WRGB VIVID III",
        ("DYVVD3",),
        WRGB_CHANNELS,
        has_fan=True,
        min_fan_speed=25,
        sea_led_family=True,
    ),
    # DYNC2N is the new-gen C-series (DYN prefix → SeaLed family).
    DeviceModel("C II", ("DYNC2N",), WHITE_CHANNELS, sea_led_family=True),
    # DYN-prefixed new-gen → SeaLed family.
    DeviceModel("C II RGB", ("DYNCRGP", "DYNCRGB"), RGB_CHANNELS, sea_led_family=True),
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
        # 550..1500 are light lengths; SeaLed per the new-gen convention.
        sea_led_family=True,
    ),
    DeviceModel("Commander 1", ("DYCOM",), COMMANDER_CHANNELS, needs_device_type=True),
    # Commander 4 exists in two generations with different device types, which
    # changes the 0x5A/0x06 auto-curve encoding: DYLED is BleLed, DYNLED is SeaLed.
    DeviceModel("Commander 4", ("DYLED",), WRGB_CHANNELS),
    DeviceModel("Commander 4", ("DYNLED",), WRGB_CHANNELS, sea_led_family=True),
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
