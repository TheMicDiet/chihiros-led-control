"""Chihiros LED control library."""

from .client import ChihirosDevice, ChihirosDosingPump
from .exceptions import UnsupportedDeviceError
from .factory import (
    create_device,
    detect_model,
    get_device_from_address,
    needs_device_type,
)
from .models import DeviceModel

__all__ = [
    "ChihirosDevice",
    "ChihirosDosingPump",
    "DeviceModel",
    "UnsupportedDeviceError",
    "create_device",
    "detect_model",
    "get_device_from_address",
    "needs_device_type",
]
