"""Module defining Chihiros devices."""

import inspect
import sys
from typing import Callable

from bleak import BleakScanner
from bleak.backends.device import BLEDevice

from ..exception import DeviceNotFound
from .a2 import AII
from .base_device import BaseDevice
from .c2 import CII
from .c2rgb import CIIRGB
from .commander1 import Commander1
from .commander4 import Commander4
from .fallback import Fallback
from .generic_rgb import GenericRGB
from .generic_white import GenericWhite
from .generic_wrgb import GenericWRGB
from .tiny_terrarium_egg import TinyTerrariumEgg
from .universal_wrgb import UniversalWRGB
from .wrgb2 import WRGBII
from .wrgb2_pro import WRGBIIPro
from .wrgb2_slim import WRGBIISlim
from .z_light_tiny import ZLightTiny

CODE2MODEL = {}
for name, obj in inspect.getmembers(sys.modules[__name__]):
    if inspect.isclass(obj) and issubclass(obj, BaseDevice):
        for model_code in obj._model_codes:
            CODE2MODEL[model_code] = obj


def get_model_class_from_name(device_name: str) -> Callable[[BLEDevice], BaseDevice]:
    """Get device class name from device name."""
    return CODE2MODEL.get(device_name[:-12], Fallback)


async def get_device_from_address(device_address: str) -> BaseDevice:
    """Get BLEDevice object from mac address."""
    # TODO Add logger
    ble_dev = await BleakScanner.find_device_by_address(device_address)
    if ble_dev and ble_dev.name is not None:
        model_class = get_model_class_from_name(ble_dev.name)
        dev: BaseDevice = model_class(ble_dev)
        return dev

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
    "FallBack",
    "BaseDevice",
    "RGBMode",
    "CODE2MODEL",
    "get_device_from_address",
    "get_model_class_from_name",
    "GenericRGB",
    "GenericWhite",
    "GenericWRGB",
]
