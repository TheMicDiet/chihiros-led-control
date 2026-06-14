"""Sensor platform for Chihiros notification data."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import (
    ATTR_FIRMWARE_VERSION,
    ATTR_RUNTIME_MINUTES,
    ATTR_SCHEDULE_POINTS,
    ChihirosDataUpdateCoordinator,
)
from .entity import chihiros_device_info, chihiros_entity_name, chihiros_unique_id
from .models import ChihirosData
from .vendor.chihiros_led_control import ChihirosDevice

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChihirosSensorDescription:
    """Description for a Chihiros notification sensor."""

    key: str
    name: str
    device_class: SensorDeviceClass | None = None
    native_unit_of_measurement: str | None = None


SENSOR_DESCRIPTIONS = (
    ChihirosSensorDescription(
        key=ATTR_FIRMWARE_VERSION,
        name="Firmware Version",
    ),
    ChihirosSensorDescription(
        key=ATTR_RUNTIME_MINUTES,
        name="Runtime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
    ),
    ChihirosSensorDescription(
        key=ATTR_SCHEDULE_POINTS,
        name="Schedule",
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
        device: ChihirosDevice,
        description: ChihirosSensorDescription,
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
    def native_value(self) -> int | str | None:
        """Return the current sensor value."""
        value = self.coordinator.data.get(self.entity_description.key)
        if self.entity_description.key == ATTR_SCHEDULE_POINTS:
            if value is None:
                return None
            return f"{len(value)} points"
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return detailed notification data."""
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
