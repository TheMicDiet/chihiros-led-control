"""LED BLE integration light platform."""

from __future__ import annotations

import logging
import math
from typing import Any

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

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
    """Set up the light platform for LEDBLE."""
    chihiros_data: ChihirosData = hass.data[DOMAIN][entry.entry_id]
    _LOGGER.debug("Setup chihiros entry: %s", chihiros_data.device.address)
    for color in chihiros_data.device.colors:
        _LOGGER.debug("Setup chihiros light entity: %s - %s", chihiros_data.device.address, color)
        async_add_entities(
            [
                ChihirosLightEntity(
                    chihiros_data.coordinator,
                    chihiros_data.device,
                    color=color,
                )
            ]
        )


class ChihirosLightEntity(
    PassiveBluetoothCoordinatorEntity[ChihirosDataUpdateCoordinator],
    LightEntity,
    RestoreEntity,
):
    """Representation of Chihiros device."""

    _attr_assumed_state = True
    _attr_should_poll = False
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(
        self,
        coordinator: ChihirosDataUpdateCoordinator,
        chihiros_device: ChihirosClient,
        color: str,
    ) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self._device = chihiros_device
        self._address = coordinator.address
        self._color = color

        self._attr_name = chihiros_entity_name(self._device, self._color)
        self._attr_unique_id = chihiros_unique_id(self._address, self._color)
        self._attr_color = self._color
        self._attr_extra_state_attributes = {"color": self._color}
        self._attr_device_info = chihiros_device_info(self._device, self._address)

    async def async_added_to_hass(self) -> None:
        """Handle entity about to be added to hass event."""
        _LOGGER.debug("Called async_added_to_hass: %s", self.name)
        await super().async_added_to_hass()
        if last_state := await self.async_get_last_state():
            self._attr_is_on = last_state.state == STATE_ON
            self._attr_brightness = last_state.attributes.get("brightness")

    @property
    def available(self) -> bool:
        """Return whether the light is available."""
        if self.coordinator.always_available:
            return True
        return super().available

    @property
    def brightness(self) -> int | None:
        """Return the brightness property."""
        return self._attr_brightness

    @property
    def color_mode(self) -> str | None:
        """Return the color mode of the light."""
        return self._attr_color_mode

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Instruct the light to turn on."""
        if ATTR_BRIGHTNESS in kwargs:
            hass_brightness = int(kwargs[ATTR_BRIGHTNESS])
            brightness = max(1, math.ceil((hass_brightness / 255) * 100))
            _LOGGER.debug("Turning on: %s to %s", self.name, brightness)
            await self._set_entity_brightness(brightness)
            self._attr_brightness = hass_brightness
        else:
            _LOGGER.debug("Turning on: %s", self.name)
            await self._set_entity_brightness(100)
            self._attr_brightness = 255
        self._attr_is_on = True
        self.schedule_update_ha_state()
        _LOGGER.debug("Turned on: %s", self.name)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Instruct the light to turn off."""
        _LOGGER.debug("Turning off: %s", self.name)
        await self._set_entity_brightness(0)
        self._attr_is_on = False
        self._attr_brightness = 0
        self.schedule_update_ha_state()
        _LOGGER.debug("Turned off: %s", self.name)

    async def _set_entity_brightness(self, brightness: int) -> None:
        """Set brightness and keep Home Assistant availability in sync."""
        try:
            await self._device.set_brightness({self._color: brightness})
        except Exception as ex:
            self._attr_available = False
            self.schedule_update_ha_state()
            raise HomeAssistantError(f"Failed to set brightness for {self.name}") from ex
        self._attr_available = True
