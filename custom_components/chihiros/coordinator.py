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
from .vendor.chihiros_led_control.protocol import (
    ParsedNotification,
    RuntimeNotification,
    SchedulePoint,
    ScheduleSnapshotNotification,
)

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

_LOGGER: logging.Logger = logging.getLogger(__name__)
ATTR_FIRMWARE_VERSION = "firmware_version"
ATTR_RUNTIME_MINUTES = "runtime_minutes"
ATTR_SCHEDULE_POINTS = "schedule_points"


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
        self._remove_notification_callback = client.add_notification_callback(self._queue_notification)
        super().__init__(
            hass,
            _LOGGER,
            ble_device.address,
            bluetooth.BluetoothScanningMode.ACTIVE,
        )

    async def async_request_status(self) -> None:
        """Request the latest runtime/status notification from the device."""
        await self.api.query_status()

    def async_close(self) -> None:
        """Remove library callbacks held by this coordinator."""
        self._remove_notification_callback()

    def _queue_notification(self, notification: ParsedNotification) -> None:
        """Queue notification handling on the Home Assistant event loop."""
        self.hass.loop.call_soon_threadsafe(self._async_handle_notification, notification)

    @callback
    def _async_handle_notification(self, notification: ParsedNotification) -> None:
        """Store parsed notification data and update entities."""
        if isinstance(notification, RuntimeNotification):
            self.data[ATTR_FIRMWARE_VERSION] = notification.firmware_version
            self.data[ATTR_RUNTIME_MINUTES] = notification.runtime_minutes
        elif isinstance(notification, ScheduleSnapshotNotification):
            self.data[ATTR_FIRMWARE_VERSION] = notification.firmware_version
            self.data[ATTR_SCHEDULE_POINTS] = tuple(_schedule_point_to_dict(point) for point in notification.points)
        self.async_set_updated_data(dict(self.data))

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
    def _async_handle_unavailable(self, service_info: bluetooth.BluetoothServiceInfoBleak) -> None:
        """Handle the device going unavailable."""
        _LOGGER.debug("%s: Chihiros device unavailable", self.ble_device.address)
        super()._async_handle_unavailable(service_info)


def _schedule_point_to_dict(point: SchedulePoint) -> dict[str, Any]:
    """Return a Home Assistant-friendly schedule point."""
    return {
        "time": f"{point.hour:02d}:{point.minute:02d}",
        "levels": dict(point.levels),
    }
