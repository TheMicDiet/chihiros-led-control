"""Fan platform for fan-equipped Chihiros devices such as the WRGB VIVID III."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import ATTR_FAN_RPM, ChihirosDataUpdateCoordinator
from .entity import chihiros_device_info, chihiros_entity_name, chihiros_unique_id
from .models import ChihirosData
from .runtime import ChihirosClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the fan platform for fan-equipped Chihiros devices."""
    chihiros_data: ChihirosData = hass.data[DOMAIN][entry.entry_id]
    if not chihiros_data.device.model.has_fan:
        return
    async_add_entities(
        [
            ChihirosFanEntity(
                chihiros_data.coordinator,
                chihiros_data.device,
            )
        ]
    )


class ChihirosFanEntity(
    PassiveBluetoothCoordinatorEntity[ChihirosDataUpdateCoordinator],
    FanEntity,
    RestoreEntity,
):
    """Representation of a Chihiros device fan."""

    _attr_assumed_state = True
    _attr_should_poll = False
    _attr_supported_features = FanEntityFeature.SET_SPEED

    def __init__(
        self,
        coordinator: ChihirosDataUpdateCoordinator,
        chihiros_device: ChihirosClient,
    ) -> None:
        """Initialize the fan entity."""
        super().__init__(coordinator)
        self._device = chihiros_device
        self._address = coordinator.address
        self._attr_name = chihiros_entity_name(self._device, "Fan")
        self._attr_unique_id = chihiros_unique_id(self._address, "fan")
        self._attr_device_info = chihiros_device_info(self._device, self._address)
        self._attr_percentage = 0

    async def async_added_to_hass(self) -> None:
        """Handle entity about to be added to hass event."""
        await super().async_added_to_hass()
        if last_state := await self.async_get_last_state():
            self._attr_percentage = last_state.attributes.get("percentage") or 0

    @property
    def available(self) -> bool:
        """Return whether the fan is available."""
        if self.coordinator.always_available:
            return True
        return super().available

    @property
    def is_on(self) -> bool:
        """Return whether the fan is running."""
        return bool(self._attr_percentage and self._attr_percentage > 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the measured fan RPM reported by status notifications."""
        fan_rpm = self.coordinator.data.get(ATTR_FAN_RPM)
        if fan_rpm is None:
            return None
        return {ATTR_FAN_RPM: fan_rpm}

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the fan speed percentage."""
        _LOGGER.debug("Setting fan speed: %s to %s%%", self.name, percentage)
        await self._set_fan_speed(percentage)
        self._attr_percentage = percentage
        self.schedule_update_ha_state()

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn on the fan."""
        del preset_mode, kwargs
        if percentage is None:
            percentage = self._attr_percentage or 100
        await self.async_set_percentage(percentage)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the fan."""
        del kwargs
        await self.async_set_percentage(0)

    async def _set_fan_speed(self, percentage: int) -> None:
        """Send the fan speed command and raise on BLE failure."""
        try:
            await self._device.set_fan_speed(percentage)
        except Exception as ex:
            raise HomeAssistantError(f"Failed to set fan speed for {self.name}") from ex
