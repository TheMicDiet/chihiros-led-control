"""Exceptions module."""


class CharacteristicMissingError(Exception):
    """Raised when a characteristic is missing."""


class DeviceNotFound(Exception):
    """Raised when BLE device is not found."""


class UnsupportedDeviceError(Exception):
    """Raised when a known non-LED device is passed to the LED client factory."""
