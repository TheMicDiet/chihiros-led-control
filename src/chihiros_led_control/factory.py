"""Device detection and construction helpers."""

from __future__ import annotations

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .devices.dosing import ChihirosDosingPump
from .devices.heater import ChihirosHeater
from .devices.led import ChihirosDevice
from .devices.stirrer import ChihirosMagStirrer
from .exceptions import DeviceNotFound, UnsupportedDeviceError
from .models import DeviceModel, DosingPumpSpec, HeaterSpec, MagStirrerSpec
from .registry import (
    GENERIC_MODELS_BY_DEVICE_TYPE,
    detect_model,
    is_known_unsupported_device,
)


def needs_device_type(device_name: str | None) -> bool:
    """Return whether a device needs a user-selected generic type."""
    return detect_model(device_name).needs_device_type


def model_for_device_type(device_type: str | None) -> DeviceModel:
    """Return a generic model for a stored device type."""
    if not device_type:
        return GENERIC_MODELS_BY_DEVICE_TYPE["white"]
    return GENERIC_MODELS_BY_DEVICE_TYPE.get(device_type, GENERIC_MODELS_BY_DEVICE_TYPE["white"])


def resolve_model(
    device_name: str | None,
    model: DeviceModel | None = None,
    device_type: str | None = None,
) -> DeviceModel:
    """Resolve final model metadata for a device."""
    detected = model or detect_model(device_name)
    if detected.needs_device_type and device_type:
        return model_for_device_type(device_type)
    return detected


def create_device(
    ble_device: BLEDevice,
    model: DeviceModel | None = None,
    device_type: str | None = None,
    advertisement_data: AdvertisementData | None = None,
):
    """Create the family driver selected by the resolved profile."""
    if is_known_unsupported_device(ble_device.name):
        raise UnsupportedDeviceError(f"Unsupported Chihiros device: {ble_device.name}")
    resolved_model = resolve_model(ble_device.name, model, device_type)
    spec_type = type(resolved_model.spec)
    if spec_type is HeaterSpec:
        return ChihirosHeater(ble_device, resolved_model, advertisement_data)
    if spec_type is MagStirrerSpec:
        return ChihirosMagStirrer(ble_device, resolved_model, advertisement_data)
    if spec_type is DosingPumpSpec:
        return ChihirosDosingPump(ble_device, resolved_model, advertisement_data)
    return ChihirosDevice(ble_device, resolved_model, advertisement_data)


async def get_device_from_address(device_address: str, device_type: str | None = None):
    """Get a device client from a BLE address."""
    ble_dev = await BleakScanner.find_device_by_address(device_address)
    if ble_dev:
        return create_device(ble_dev, device_type=device_type)
    raise DeviceNotFound


__all__ = [
    "create_device",
    "detect_model",
    "get_device_from_address",
    "is_known_unsupported_device",
    "model_for_device_type",
    "needs_device_type",
    "resolve_model",
]
