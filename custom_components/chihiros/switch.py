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
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

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
    if not chihiros_data.device.colors:
        return
    entities: list[SwitchEntity] = [
        ChihirosAutoManualSwitch(
            chihiros_data.coordinator,
            chihiros_data.device,
        )
    ]
    if chihiros_data.device.model.is_vivid3:
        entities.append(
            ChihirosVivid3Switch(
                chihiros_data.device,
                "temp_protect",
                "set_temp_protect",
                "Temperature Protection",
            )
        )
        entities.append(
            ChihirosVivid3Switch(
                chihiros_data.device,
                "bluetooth_led",
                "set_bluetooth_led",
                "Indicator LED",
            )
        )
    async_add_entities(entities)


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
        return self.coordinator.auto_mode

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Auto mode: set brightness to auto level and enable auto mode."""
        await self._device.enable_auto_mode(dt_util.now())
        self.coordinator.async_set_auto_mode(True)
        self.async_write_ha_state()
        _LOGGER.debug("Switched to auto mode for %s", self._device.name)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Manual mode: set brightness to last known or default value."""
        await self._device.set_manual_mode()
        self.coordinator.async_set_auto_mode(False)
        self.async_write_ha_state()
        _LOGGER.debug("Switched to manual mode for %s", self._device.name)


class ChihirosVivid3Switch(SwitchEntity, RestoreEntity):
    """Optimistic VIVID III switch (temperature protection / indicator LED).

    The device sends no notification for these settings, so the state is
    tracked optimistically and restored across HA restarts. The restored value
    is *not* re-sent to the device: the hardware default is unknown, and a
    surprise write on startup could contradict what the device actually uses.
    """

    _attr_should_poll = False

    def __init__(
        self,
        device: ChihirosClient,
        state_property: str,
        setter_name: str,
        name_suffix: str,
    ) -> None:
        """Initialize the switch."""
        self._device = device
        self._state_property = state_property
        self._setter_name = setter_name
        self._restored_state: bool | None = None
        self._attr_name = chihiros_entity_name(device, name_suffix)
        self._attr_unique_id = chihiros_unique_id(device.address, state_property)
        self._attr_device_info = chihiros_device_info(device, device.address)

    async def async_added_to_hass(self) -> None:
        """Restore the last known state."""
        await super().async_added_to_hass()
        if last_state := await self.async_get_last_state():
            self._restored_state = last_state.state == "on"

    @property
    def is_on(self) -> bool:
        """Return the optimistic switch state."""
        if self._restored_state is not None:
            return self._restored_state
        return bool(getattr(self._device, self._state_property))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the setting on the device."""
        await getattr(self._device, self._setter_name)(True)
        self._restored_state = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the setting on the device."""
        await getattr(self._device, self._setter_name)(False)
        self._restored_state = False
        self.async_write_ha_state()
