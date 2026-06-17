"""Sensor platform for Chihiros notification data."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import (
    ATTR_FIRMWARE_VERSION,
    ATTR_LAST_NOTIFICATION,
    ATTR_SCHEDULE_POINTS,
    ChihirosDataUpdateCoordinator,
)
from .entity import chihiros_device_info, chihiros_entity_name, chihiros_unique_id
from .models import ChihirosData
from .runtime import ChihirosClient

_LOGGER = logging.getLogger(__name__)
MAX_SENSOR_STATE_LENGTH = 255


SENSOR_DESCRIPTIONS = (
    SensorEntityDescription(
        key=ATTR_FIRMWARE_VERSION,
        name="Firmware Version",
    ),
    SensorEntityDescription(
        key=ATTR_SCHEDULE_POINTS,
        name="Schedule",
    ),
    SensorEntityDescription(
        key=ATTR_LAST_NOTIFICATION,
        name="Last Notification",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up notification sensors for Chihiros LED Control."""
    chihiros_data: ChihirosData = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            ChihirosNotificationSensor(
                chihiros_data.coordinator,
                chihiros_data.device,
                description,
            )
            for description in SENSOR_DESCRIPTIONS
        ]
    )
    hass.async_create_task(_async_request_initial_status(chihiros_data.coordinator))


async def _async_request_initial_status(coordinator: ChihirosDataUpdateCoordinator) -> None:
    """Request an initial notification snapshot without blocking setup."""
    try:
        await coordinator.async_request_status()
    except Exception:
        _LOGGER.debug("Failed to request initial Chihiros status", exc_info=True)


class ChihirosNotificationSensor(
    PassiveBluetoothCoordinatorEntity[ChihirosDataUpdateCoordinator],
    SensorEntity,
):
    """Sensor backed by parsed Chihiros notification data."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: ChihirosDataUpdateCoordinator,
        device: ChihirosClient,
        description: SensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._device = device
        self._attr_name = chihiros_entity_name(device, description.name)
        self._attr_unique_id = chihiros_unique_id(coordinator.address, description.key)
        self._attr_device_info = chihiros_device_info(device, coordinator.address)
        self._attr_device_class = description.device_class
        self._attr_native_unit_of_measurement = description.native_unit_of_measurement

    @property
    def available(self) -> bool:
        """Return whether the sensor is available."""
        if self.coordinator.always_available:
            return True
        return super().available

    @property
    def native_value(self) -> int | str | None:
        """Return the current sensor value."""
        value = self.coordinator.data.get(self.entity_description.key)
        if self.entity_description.key == ATTR_LAST_NOTIFICATION:
            if not isinstance(value, dict):
                return None
            return value.get("mode")
        if self.entity_description.key == ATTR_SCHEDULE_POINTS:
            if value is None:
                return None
            return _format_schedule_state(value)
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return detailed notification data."""
        if self.entity_description.key == ATTR_LAST_NOTIFICATION:
            notification = self.coordinator.data.get(ATTR_LAST_NOTIFICATION)
            if not isinstance(notification, dict):
                return None
            return notification
        if self.entity_description.key != ATTR_SCHEDULE_POINTS:
            return None
        points = self.coordinator.data.get(ATTR_SCHEDULE_POINTS)
        if points is None:
            return None
        return {"points": points}

    async def async_update(self) -> None:
        """Ask the device for a fresh status notification."""
        try:
            await self.coordinator.async_request_status()
        except Exception as ex:
            raise HomeAssistantError(f"Failed to request status for {self._device.name}") from ex


def _format_schedule_state(points: tuple[dict[str, Any], ...]) -> str:
    """Return a compact display value for schedule points."""
    if not points:
        return "No schedule"
    formatted_points = [_format_schedule_point(point) for point in points]
    schedule = "; ".join(formatted_points)
    if len(schedule) <= MAX_SENSOR_STATE_LENGTH:
        return schedule
    return f"{len(points)} points"


def _format_schedule_point(point: dict[str, Any]) -> str:
    """Return one compact schedule point."""
    levels = point.get("levels", {})
    if not isinstance(levels, dict) or not levels:
        return str(point.get("time", "unknown"))
    unique_levels = set(levels.values())
    if len(unique_levels) == 1:
        return f"{point.get('time', 'unknown')} {unique_levels.pop()}%"
    level_text = "/".join(f"{color[:1].upper()}{level}" for color, level in levels.items())
    return f"{point.get('time', 'unknown')} {level_text}"
