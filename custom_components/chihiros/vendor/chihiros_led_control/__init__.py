"""Chihiros LED control library."""

from .devices import ChihirosDevice, ChihirosDosingPump, ChihirosHeater, ChihirosMagStirrer
from .exceptions import UnsupportedDeviceError
from .factory import (
    create_device,
    detect_model,
    get_device_from_address,
    needs_device_type,
    resolve_model,
)
from .models import DeviceKind, DeviceModel, LedFeature, LedProtocol

__all__ = [
    "ChihirosDevice",
    "ChihirosDosingPump",
    "ChihirosHeater",
    "ChihirosMagStirrer",
    "DeviceKind",
    "DeviceModel",
    "LedFeature",
    "LedProtocol",
    "UnsupportedDeviceError",
    "create_device",
    "detect_model",
    "get_device_from_address",
    "needs_device_type",
    "resolve_model",
]
