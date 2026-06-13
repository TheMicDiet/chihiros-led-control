"""Integration to integrate Keymitt BLE devices with Home Assistant."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothDataUpdateCoordinator,
)
from homeassistant.core import HomeAssistant, callback

from .vendor.chihiros_led_control import ChihirosDevice

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

_LOGGER: logging.Logger = logging.getLogger(__name__)


class ChihirosDataUpdateCoordinator(PassiveBluetoothDataUpdateCoordinator):
    """Coordinator that tracks passive Bluetooth availability events."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: ChihirosDevice,
        ble_device: BLEDevice,
    ) -> None:
        """Initialize."""
        self.api: ChihirosDevice = client
        self.data: dict[str, Any] = {}
        self.ble_device = ble_device
        super().__init__(
            hass,
            _LOGGER,
            ble_device.address,
            bluetooth.BluetoothScanningMode.ACTIVE,
        )

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Handle a Bluetooth event."""
        _LOGGER.debug("%s: Bluetooth event: %s", self.ble_device.address, change)
        super()._async_handle_bluetooth_event(service_info, change)

    @callback
    def _async_handle_unavailable(
        self, service_info: bluetooth.BluetoothServiceInfoBleak
    ) -> None:
        """Handle the device going unavailable."""
        _LOGGER.debug("%s: Chihiros device unavailable", self.ble_device.address)
        super()._async_handle_unavailable(service_info)
