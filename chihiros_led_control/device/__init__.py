"""Module defining Chihiros devices."""

import inspect
from typing import Callable

from bleak import BleakScanner
from bleak.backends.device import BLEDevice

from chihiros_led_control import device
from chihiros_led_control.device.base_device import BaseDevice
from chihiros_led_control.device.fallback import Fallback
from chihiros_led_control.device.tiny_terrarium_egg import TinyTerrariumEgg

CODE2MODEL = {}
for sub in dir(device):
    attr = getattr(device, sub)
    if inspect.isclass(attr) and issubclass(attr, BaseDevice):
        CODE2MODEL[attr._code] = attr


def get_model_class_from_name(device_name: str) -> Callable[[BLEDevice], BaseDevice]:
    """Get device class name from device name."""
    return CODE2MODEL.get(device_name[:-12], Fallback)


async def get_device_from_address(device_address: str) -> BaseDevice:
    """Get BLEDevice object from mac address."""
    # TODO Add logger
    ble_dev = await BleakScanner.find_device_by_address(device_address)  # type: ignore
    if ble_dev and ble_dev.name is not None:
        model_class = get_model_class_from_name(ble_dev.name)
        dev: BaseDevice = model_class(ble_dev)
        return dev

    raise


__all__ = [
    "TinyTerrariumEgg",
    "FallBack",
    "BaseDevice",
    "CODE2MODEL",
    "get_device_from_address",
    "get_model_class_from_name",
]
