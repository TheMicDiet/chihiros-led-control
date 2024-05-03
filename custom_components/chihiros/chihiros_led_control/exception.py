"""Exceptions module."""


class CharacteristicMissingError(Exception):
    """Raised when a characteristic is missing."""


class DeviceNotFound(Exception):
    """Raised when BLE device is not found."""
