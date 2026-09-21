"""Device profile registry and advertised-name resolution."""

from __future__ import annotations

from types import MappingProxyType

from .models import (
    COMMANDER_CHANNELS,
    RGB_CHANNELS,
    TINY_TERRARIUM_EGG_CHANNELS,
    WHITE_CHANNELS,
    WRGB_CHANNELS,
    X300_CHANNELS,
    Z_LIGHT_TINY_CHANNELS,
    DeviceModel,
    DosingPumpSpec,
    HeaterSpec,
    LedFeature,
    LedProtocol,
    LedSpec,
    MagStirrerSpec,
)


def _led(
    name: str,
    codes: tuple[str, ...],
    channels,
    *,
    protocol: LedProtocol = LedProtocol.BLE_LED,
    features: frozenset[LedFeature] = frozenset(),
    min_fan_speed: int = 0,
    needs_device_type: bool = False,
    fallback: bool = False,
) -> DeviceModel:
    return DeviceModel(
        name,
        codes,
        LedSpec(channels, protocol, features, min_fan_speed),
        needs_device_type=needs_device_type,
        fallback=fallback,
    )


GENERIC_WHITE = _led("Generic White LED", (), WHITE_CHANNELS)
GENERIC_RGB = _led("Generic RGB", (), RGB_CHANNELS)
GENERIC_WRGB = _led("Generic WRGB", (), WRGB_CHANNELS)
FALLBACK = _led("fallback", (), COMMANDER_CHANNELS, needs_device_type=True, fallback=True)
DOSING_PUMP = DeviceModel("Dosing Pump", ("DYDOSE", "DYDOSED", "DYTDOS", "DYNDOS"), DosingPumpSpec())
MAG_STIRRER = DeviceModel("Mag Stirrer", ("DYMIXR",), MagStirrerSpec())
HEATER = DeviceModel("Heater", ("DYHET", "DYH1T"), HeaterSpec())

SUPPORTED_MODELS: tuple[DeviceModel, ...] = (
    _led("Z Light TINY", ("DYSSD", "DYZSD"), Z_LIGHT_TINY_CHANNELS),
    _led("Tiny Terrarium Egg", ("DYDD",), TINY_TERRARIUM_EGG_CHANNELS),
    _led("A II", ("DYNA2", "DYNA2N"), WHITE_CHANNELS, protocol=LedProtocol.SEA_LED),
    _led("A Series", ("DYA",), WHITE_CHANNELS),
    _led("New C", ("DYC",), WHITE_CHANNELS),
    _led("New C", ("DYNC2",), WHITE_CHANNELS, protocol=LedProtocol.SEA_LED),
    _led("RGB+APLUS", ("DYARGB", "DYRGBA+", "DYRGBA"), RGB_CHANNELS),
    _led("RGB+APLUS", ("DYNARGB",), RGB_CHANNELS, protocol=LedProtocol.SEA_LED),
    _led("RGB VIVID", ("DYREE",), RGB_CHANNELS),
    _led("RGB VIVID II", ("DYRGBV",), RGB_CHANNELS, protocol=LedProtocol.NEW_BLE_LED),
    _led("RGB VIVID II", ("DYNVVD", "DYNV"), RGB_CHANNELS, protocol=LedProtocol.SEA_LED),
    _led("SEA_LED", ("DYSEA",), WRGB_CHANNELS, protocol=LedProtocol.SEA_LED),
    _led("Commander X", ("DYONE",), WHITE_CHANNELS),
    _led("X300", ("DYTWO",), X300_CHANNELS),
    _led("WRGB II", ("DYWRGB",), RGB_CHANNELS),
    _led(
        "WRGB II",
        ("DYNT90", "DYNW30", "DYNW45", "DYNW60", "DYNW90", "DYNW12P", "DYNWRGB"),
        RGB_CHANNELS,
        protocol=LedProtocol.SEA_LED,
    ),
    _led(
        "WRGB II Pro",
        ("DYWPRO30", "DYWPRO45", "DYWPRO60", "DYWPRO80", "DYWPRO90", "DYWPR120"),
        WRGB_CHANNELS,
        protocol=LedProtocol.SEA_LED,
    ),
    _led(
        "WRGB II Slim",
        ("DYSILN", "DYSL30", "DYSL45", "DYSL60", "DYSL90", "DYSL120", "DYSL12"),
        RGB_CHANNELS,
        protocol=LedProtocol.SEA_LED,
    ),
    _led(
        "WRGB VIVID III",
        ("DYVVD3",),
        WRGB_CHANNELS,
        protocol=LedProtocol.SEA_LED,
        features=frozenset({LedFeature.FAN, LedFeature.TEMPERATURE_PROTECTION, LedFeature.INDICATOR_LED}),
        min_fan_speed=25,
    ),
    _led("C II", ("DYNC2N",), WHITE_CHANNELS, protocol=LedProtocol.SEA_LED),
    _led("C II RGB", ("DYNCRGP", "DYNCRGB"), RGB_CHANNELS, protocol=LedProtocol.SEA_LED),
    _led(
        "Universal WRGB",
        ("DYU550", "DYU600", "DYU700", "DYU800", "DYU920", "DYU1000", "DYU1200", "DYU1500"),
        WRGB_CHANNELS,
        protocol=LedProtocol.SEA_LED,
    ),
    _led("Commander 1", ("DYCOM",), COMMANDER_CHANNELS, needs_device_type=True),
    _led("Commander 4", ("DYLED",), WRGB_CHANNELS),
    _led("Commander 4", ("DYNLED",), WRGB_CHANNELS, protocol=LedProtocol.SEA_LED),
    DOSING_PUMP,
    MAG_STIRRER,
    HEATER,
)

GENERIC_MODELS_BY_DEVICE_TYPE = MappingProxyType(
    {"white": GENERIC_WHITE, "rgb": GENERIC_RGB, "wrgb": GENERIC_WRGB}
)
MODEL_BY_CODE = MappingProxyType({code: model for model in SUPPORTED_MODELS for code in model.advertised_codes})
KNOWN_UNSUPPORTED_DEVICE_PREFIXES = ("DYAPRCO2", "DYCHIL", "DYCO2")


def iter_model_codes_by_specificity() -> tuple[tuple[str, DeviceModel], ...]:
    """Return model codes sorted so longer prefixes win."""
    return tuple(sorted(MODEL_BY_CODE.items(), key=lambda code_model: len(code_model[0]), reverse=True))


def is_known_unsupported_device(device_name: str | None) -> bool:
    """Return whether an advertised name belongs to a known unsupported family."""
    return bool(device_name and any(device_name.startswith(prefix) for prefix in KNOWN_UNSUPPORTED_DEVICE_PREFIXES))
def detect_model(device_name: str | None) -> DeviceModel:
    """Detect a profile from an advertised device name."""
    if not device_name or is_known_unsupported_device(device_name):
        return FALLBACK
    for advertised_code, model in iter_model_codes_by_specificity():
        if device_name.startswith(advertised_code):
            return model
    return FALLBACK
