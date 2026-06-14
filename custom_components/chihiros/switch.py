"""Switch platform for Chihiros LED Control to toggle auto/manual mode."""

import logging
from typing import Any

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ChihirosDataUpdateCoordinator
from .entity import chihiros_device_info, chihiros_entity_name, chihiros_unique_id
from .models import ChihirosData
from .runtime import ChihirosClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the switch platform for Chihiros LED Control."""
    chihiros_data: ChihirosData = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            ChihirosAutoManualSwitch(
                chihiros_data.coordinator,
                chihiros_data.device,
            )
        ]
    )


class ChihirosAutoManualSwitch(
    PassiveBluetoothCoordinatorEntity[ChihirosDataUpdateCoordinator],
    SwitchEntity,
):
    """Switch to toggle between auto and manual mode."""

    def __init__(
        self,
        coordinator: ChihirosDataUpdateCoordinator,
        device: ChihirosClient,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._device = device
        self._attr_name = chihiros_entity_name(device, "Auto Mode")
        self._attr_unique_id = chihiros_unique_id(coordinator.address, "auto_mode")
        self._attr_is_on = False
        self._attr_device_info = chihiros_device_info(device, coordinator.address)

    @property
    def available(self) -> bool:
        """Return whether the switch is available."""
        if self.coordinator.always_available:
            return True
        return super().available

    @property
    def is_on(self) -> bool:
        """Return True if the switch is in auto mode."""
        return self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Auto mode: set brightness to auto level and enable auto mode."""
        await self._device.enable_auto_mode()
        self._attr_is_on = True
        self.async_write_ha_state()
        _LOGGER.debug("Switched to auto mode for %s", self._device.name)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Manual mode: set brightness to last known or default value."""
        await self._device.set_manual_mode()
        self._attr_is_on = False
        self.async_write_ha_state()
        _LOGGER.debug("Switched to manual mode for %s", self._device.name)
