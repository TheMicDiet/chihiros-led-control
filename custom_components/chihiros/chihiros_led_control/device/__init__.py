"""Module defining Chihiros devices."""
from __future__ import annotations

import inspect
import sys
from typing import Type

from bleak import BleakScanner
from bleak.backends.device import BLEDevice

from ..exception import DeviceNotFound
from .base_device import BaseDevice

# Import all known model classes so they are present in this module's namespace
from .a2 import AII
from .c2 import CII
from .c2rgb import CIIRGB
from .fallback import Fallback
from .tiny_terrarium_egg import TinyTerrariumEgg
from .wrgb2 import WRGBII
from .wrgb2_slim import WRGBIISlim
from .z_light_tiny import ZLightTiny
from ...chihiros_led_ch4_control.device.commander1 import Commander1
from ...chihiros_led_ch4_control.device.commander4 import Commander4
from ...chihiros_led_ch4_control.device.wrgb2_pro import WRGBIIPro
from ...chihiros_led_ch4_control.device.universal_wrgb import UniversalWRGB

# NEW: include the doser stub so discovery resolves to "Doser" instead of "fallback"
from .doser import Doser  # make sure this file exists with _model_codes like ["DYDOSED2", "DYDOSED", "DYDOSE"]

# Build a mapping of MODEL_CODE -> class by introspecting imported classes
CODE2MODEL: dict[str, Type[BaseDevice]] = {}
for _name, _obj in inspect.getmembers(sys.modules[__name__]):
    if inspect.isclass(_obj) and issubclass(_obj, BaseDevice):
        for _code in getattr(_obj, "_model_codes", []):
            if isinstance(_code, str) and _code:
                CODE2MODEL[_code.upper()] = _obj


def get_model_class_from_name(device_name: str) -> Type[BaseDevice]:
    """Return the device class for a given BLE advertised name.

    Matches by prefix so names like 'DYDOSED203E0FEFCBC' resolve with codes
    ['DYDOSED2', 'DYDOSED', 'DYDOSE'].
    """
    if not device_name:
        return Fallback
    up = device_name.upper()

    # Exact match first
    if up in CODE2MODEL:
        return CODE2MODEL[up]

    # Prefix match: prefer the longest matching code
    best_cls: Type[BaseDevice] | None = None
    best_len = -1
    for code, cls in CODE2MODEL.items():
        if up.startswith(code) and len(code) > best_len:
            best_cls = cls
            best_len = len(code)
    return best_cls or Fallback


async def get_device_from_address(device_address: str) -> BaseDevice:
    """Instantiate the correct device class from a MAC address."""
    ble_dev = await BleakScanner.find_device_by_address(device_address)
    if ble_dev and ble_dev.name:
        model_class = get_model_class_from_name(ble_dev.name)
        return model_class(ble_dev)
    raise DeviceNotFound


__all__ = [
    "ZLightTiny",
    "TinyTerrariumEgg",
    "AII",
    "Commander1",
    "Commander4",
    "WRGBII",
    "WRGBIIPro",
    "WRGBIISlim",
    "CII",
    "CIIRGB",
    "UniversalWRGB",
    "Doser",
    "Fallback",
    "BaseDevice",
    "CODE2MODEL",
    "get_device_from_address",
    "get_model_class_from_name",
]
