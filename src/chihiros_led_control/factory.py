"""Device detection and construction helpers."""

from __future__ import annotations

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .client import ChihirosDevice
from .exceptions import DeviceNotFound
from .models import (
    FALLBACK,
    GENERIC_MODELS_BY_DEVICE_TYPE,
    DeviceModel,
    iter_model_codes_by_specificity,
)


def detect_model(device_name: str | None) -> DeviceModel:
    """Detect a device model from a BLE advertised name."""
    if not device_name:
        return FALLBACK
    for advertised_code, model in iter_model_codes_by_specificity():
        if device_name.startswith(advertised_code):
            return model
    return FALLBACK


def needs_device_type(device_name: str | None) -> bool:
    """Return whether a device needs a user-selected generic type."""
    return detect_model(device_name).needs_device_type


def model_for_device_type(device_type: str | None) -> DeviceModel:
    """Return a generic model for a stored device type."""
    if not device_type:
        return GENERIC_MODELS_BY_DEVICE_TYPE["white"]
    return GENERIC_MODELS_BY_DEVICE_TYPE.get(
        device_type, GENERIC_MODELS_BY_DEVICE_TYPE["white"]
    )


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
) -> ChihirosDevice:
    """Create a device client for a BLE device."""
    resolved_model = resolve_model(ble_device.name, model, device_type)
    return ChihirosDevice(ble_device, resolved_model, advertisement_data)


async def get_device_from_address(
    device_address: str, device_type: str | None = None
) -> ChihirosDevice:
    """Get a device client from a BLE address."""
    ble_dev = await BleakScanner.find_device_by_address(device_address)
    if ble_dev:
        return create_device(ble_dev, device_type=device_type)

    raise DeviceNotFound
