"""Sensor platform for Chihiros notification data."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import REVOLUTIONS_PER_MINUTE, UnitOfTemperature, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import (
    ATTR_DOSING_DAILY_UL,
    ATTR_DOSING_LIFETIME_UL,
    ATTR_FAN_RPM,
    ATTR_FAN_TEMPERATURE_CELSIUS,
    ATTR_FIRMWARE_VERSION,
    ATTR_LAST_NOTIFICATION,
    ATTR_SCHEDULE_POINTS,
    ChihirosDataUpdateCoordinator,
)
from .dosing import DosingDailyTotals
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

FAN_SENSOR_DESCRIPTIONS = (
    SensorEntityDescription(
        key=ATTR_FAN_RPM,
        name="Fan Speed",
        native_unit_of_measurement=REVOLUTIONS_PER_MINUTE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    SensorEntityDescription(
        key=ATTR_FAN_TEMPERATURE_CELSIUS,
        name="Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up notification sensors for Chihiros LED Control."""
    chihiros_data: ChihirosData = hass.data[DOMAIN][entry.entry_id]
    if chihiros_data.dosing_totals:
        totals = chihiros_data.dosing_totals
        entities: list[SensorEntity] = []
        for pump_idx in range(totals.pump_count):
            entities.append(
                ChihirosDosingDailyTotalSensor(chihiros_data.coordinator, chihiros_data.device, totals, pump_idx)
            )
            entities.append(
                ChihirosDosingLifetimeTotalSensor(chihiros_data.coordinator, chihiros_data.device, totals, pump_idx)
            )
            entities.append(
                ChihirosDosingLifetimeCyclesSensor(chihiros_data.coordinator, chihiros_data.device, totals, pump_idx)
            )
        async_add_entities(entities)
        return

    async_add_entities(
        ChihirosNotificationSensor(
            chihiros_data.coordinator,
            chihiros_data.device,
            description,
        )
        for description in SENSOR_DESCRIPTIONS
    )
    if chihiros_data.device.model.has_fan:
        async_add_entities(
            ChihirosNotificationSensor(
                chihiros_data.coordinator,
                chihiros_data.device,
                description,
                entity_category=None,
            )
            for description in FAN_SENSOR_DESCRIPTIONS
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
        entity_category: EntityCategory | None = EntityCategory.DIAGNOSTIC,
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
        self._attr_state_class = description.state_class
        self._attr_entity_category = entity_category

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


class ChihirosDosingSensorBase(SensorEntity):
    """Shared base for locally tracked dosing counters."""

    _attr_should_poll = False

    def __init__(
        self,
        coordinator: ChihirosDataUpdateCoordinator,
        device: ChihirosClient,
        totals: DosingDailyTotals,
        pump_idx: int,
        unique_id_suffix: str,
        name_suffix: str,
    ) -> None:
        """Initialize the dosing counter sensor."""
        self._device = device
        self._coordinator = coordinator
        self._totals = totals
        self._pump_idx = pump_idx
        self._attr_name = chihiros_entity_name(device, name_suffix)
        self._attr_unique_id = chihiros_unique_id(coordinator.address, unique_id_suffix)
        self._attr_device_info = chihiros_device_info(device, coordinator.address)

    async def async_added_to_hass(self) -> None:
        """Subscribe to dosing total updates."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._totals.address_signal, self.async_write_ha_state)
        )


class ChihirosDosingDailyTotalSensor(ChihirosDosingSensorBase):
    """Sensor for locally tracked manual dosing total for today."""

    _attr_device_class = SensorDeviceClass.VOLUME
    _attr_native_unit_of_measurement = UnitOfVolume.MILLILITERS
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: ChihirosDataUpdateCoordinator,
        device: ChihirosClient,
        totals: DosingDailyTotals,
        pump_idx: int,
    ) -> None:
        """Initialize the daily dosing total sensor."""
        pump_number = pump_idx + 1
        super().__init__(
            coordinator,
            device,
            totals,
            pump_idx,
            f"dosing_pump_{pump_number}_dosed_today",
            f"Pump {pump_number} dosed today",
        )

    @property
    def native_value(self) -> float:
        """Return today's tracked total."""
        return self._totals.total_ml(self._pump_idx)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the pump-reported dosed-today value when a notification arrived."""
        device_value = _dosing_ul_value(self._coordinator.data.get(ATTR_DOSING_DAILY_UL), self._pump_idx)
        if device_value is None:
            return None
        return {"device_dosed_today_ml": device_value}


class ChihirosDosingLifetimeTotalSensor(ChihirosDosingSensorBase):
    """Sensor for the lifetime dosed volume of one pump."""

    _attr_device_class = SensorDeviceClass.VOLUME
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.MILLILITERS
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: ChihirosDataUpdateCoordinator,
        device: ChihirosClient,
        totals: DosingDailyTotals,
        pump_idx: int,
    ) -> None:
        """Initialize the lifetime dosing volume sensor."""
        pump_number = pump_idx + 1
        super().__init__(
            coordinator,
            device,
            totals,
            pump_idx,
            f"dosing_pump_{pump_number}_total_ml",
            f"Pump {pump_number} total ml",
        )

    @property
    def native_value(self) -> float:
        """Return the lifetime tracked total volume."""
        return self._totals.lifetime_ml(self._pump_idx)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the pump-reported lifetime total when a notification arrived."""
        device_value = _dosing_ul_value(self._coordinator.data.get(ATTR_DOSING_LIFETIME_UL), self._pump_idx)
        if device_value is None:
            return None
        return {"device_total_ml": device_value}


class ChihirosDosingLifetimeCyclesSensor(ChihirosDosingSensorBase):
    """Sensor for the lifetime dose count of one pump."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "cycles"
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        coordinator: ChihirosDataUpdateCoordinator,
        device: ChihirosClient,
        totals: DosingDailyTotals,
        pump_idx: int,
    ) -> None:
        """Initialize the lifetime dosing cycles sensor."""
        pump_number = pump_idx + 1
        super().__init__(
            coordinator,
            device,
            totals,
            pump_idx,
            f"dosing_pump_{pump_number}_total_cycles",
            f"Pump {pump_number} total cycles",
        )

    @property
    def native_value(self) -> int:
        """Return the lifetime tracked dose count."""
        return self._totals.lifetime_cycles(self._pump_idx)


def _dosing_ul_value(values: object, pump_idx: int) -> float | None:
    """Return one pump's device-reported microliter counter in mL, if present."""
    if not isinstance(values, (list, tuple)) or pump_idx >= len(values):
        return None
    value = values[pump_idx]
    if not isinstance(value, (int, float)):
        return None
    return round(value / 1000, 1)


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
