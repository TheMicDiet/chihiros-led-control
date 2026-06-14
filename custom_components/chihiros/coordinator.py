"""Integration to integrate Keymitt BLE devices with Home Assistant."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothDataUpdateCoordinator,
)
from homeassistant.core import HomeAssistant, callback

from .runtime import ChihirosClient
from .vendor.chihiros_led_control.protocol import (
    ParsedNotification,
    RuntimeNotification,
    SchedulePoint,
    ScheduleSnapshotNotification,
)

_LOGGER: logging.Logger = logging.getLogger(__name__)
ATTR_FIRMWARE_VERSION = "firmware_version"
ATTR_RUNTIME_MINUTES = "runtime_minutes"
ATTR_SCHEDULE_POINTS = "schedule_points"


class ChihirosDataUpdateCoordinator(PassiveBluetoothDataUpdateCoordinator):
    """Coordinator that tracks passive Bluetooth availability events."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: ChihirosClient,
        address: str,
        always_available: bool = False,
    ) -> None:
        """Initialize."""
        self.api: ChihirosClient = client
        self.data: dict[str, Any] = {}
        self._device_address = address
        self.always_available = always_available
        self._remove_notification_callback = client.add_notification_callback(self._queue_notification)
        super().__init__(
            hass,
            _LOGGER,
            address,
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
        self.async_update_listeners()

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Handle a Bluetooth event."""
        _LOGGER.debug("%s: Bluetooth event: %s", self._device_address, change)
        super()._async_handle_bluetooth_event(service_info, change)

    @callback
    def _async_handle_unavailable(self, service_info: bluetooth.BluetoothServiceInfoBleak) -> None:
        """Handle the device going unavailable."""
        _LOGGER.debug("%s: Chihiros device unavailable", self._device_address)
        super()._async_handle_unavailable(service_info)


def _schedule_point_to_dict(point: SchedulePoint) -> dict[str, Any]:
    """Return a Home Assistant-friendly schedule point."""
    return {
        "time": f"{point.hour:02d}:{point.minute:02d}",
        "levels": dict(point.levels),
    }
