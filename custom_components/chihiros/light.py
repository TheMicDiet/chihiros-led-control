"""LED BLE integration light platform."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .chihiros_led_control.device import BaseDevice
from .const import DOMAIN, MANUFACTURER
from .coordinator import ChihirosDataUpdateCoordinator
from .models import ChihirosData

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
        _LOGGER.debug(
            "Setup chihiros light entity: %s - %s", chihiros_data.device.address, color
        )
        async_add_entities(
            [
                ChihirosLightEntity(
                    chihiros_data.coordinator,
                    chihiros_data.device,
                    entry,
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
        chihiros_device: BaseDevice,
        config_entry: ConfigEntry,
        color: str,
    ) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self._device = chihiros_device
        self._address = coordinator.address
        self._color = color

        self._attr_name = f"{self._device.name} {self._color}"
        self._attr_unique_id = f"{self._address}_{self._color}"
        self._attr_color = self._color
        self._attr_extra_state_attributes = {"color": self._color}

        model_name: str = self._device.model_name
        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_BLUETOOTH, self._address)},
            manufacturer=MANUFACTURER,
            model=model_name,
            name=self._device.name,
        )

    async def async_added_to_hass(self) -> None:
        """Handle entity about to be added to hass event."""
        _LOGGER.debug("Called async_added_to_hass: %s", self.name)
        await super().async_added_to_hass()
        if last_state := await self.async_get_last_state():
            self._attr_is_on = last_state.state == STATE_ON
            self._attr_brightness = last_state.attributes.get("brightness")

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
            brightness = int((kwargs[ATTR_BRIGHTNESS] / 255) * 100)
            _LOGGER.debug("Turning on: %s to %s", self.name, brightness)
            # TODO: handle error and availability False
            await self._device.set_color_brightness(brightness, self._color)
            self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
        else:
            _LOGGER.debug("Turning on: %s", self.name)
            await self._device.set_color_brightness(100, self._color)
        self._attr_is_on = True
        self._attr_available = True
        self.schedule_update_ha_state()
        _LOGGER.debug("Turned on: %s", self.name)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Instruct the light to turn off."""
        _LOGGER.debug("Turning off: %s", self.name)
        # TODO handle error and availability False
        await self._device.set_color_brightness(0, self._color)
        self._attr_is_on = False
        self._attr_brightness = 0
        self._attr_available = True
        self.schedule_update_ha_state()
        _LOGGER.debug("Turned off: %s", self.name)
