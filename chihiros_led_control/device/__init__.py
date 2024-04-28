import inspect

from bleak import BleakScanner

from chihiros_led_control import device
from chihiros_led_control.device.base_device import BaseDevice
from chihiros_led_control.device.fallback import Fallback
from chihiros_led_control.device.tiny_terrarium_egg import TinyTerrariumEgg

CODE2MODEL = {}
for sub in dir(device):
    attr = getattr(device, sub)
    if inspect.isclass(attr) and issubclass(attr, BaseDevice):
        CODE2MODEL[attr._code] = attr


async def get_device_from_address(device_address: str) -> BaseDevice:
    # TODO Add logger
    ble_dev = await BleakScanner.find_device_by_address(
        device_address, macos=dict(use_bdaddr=True)
    )
    if ble_dev and ble_dev.name is not None:
        device_class = CODE2MODEL.get(ble_dev.name[:-12], Fallback)
        dev = device_class(ble_dev)
    return dev


__all__ = [
    "TinyTerrariumEgg",
    "FallBack",
    "CODE2MODEL",
    "get_device_from_address",
]
