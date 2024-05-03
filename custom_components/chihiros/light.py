"""LED BLE integration light platform."""

from __future__ import annotations

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

# from led_ble import LEDBLE


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the light platform for LEDBLE."""
    chihiros_data: ChihirosData = hass.data[DOMAIN][entry.entry_id]
    for color in chihiros_data.device.colors:
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
        # self._attr_is_on = False

    #    @property
    #    def data(self) -> dict[str, Any]:
    #        """Return coordinator data for this entity.
    #
    #        TODO: Seems useless
    #        """
    #        print("AAADDDDDDD" * 22)
    #        return self.coordinator.data

    async def async_added_to_hass(self) -> None:
        """Handle entity about to be added to hass event."""
        # import ipdb;ipdb.set_trace()
        print("async_added_to_hass")
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
        print("TURNON")
        if ATTR_BRIGHTNESS in kwargs:
            brightness = int((kwargs[ATTR_BRIGHTNESS] / 255) * 100)
            print(f"brightness {brightness}")
            # TODO: handle error and availability False
            await self._device.set_color_brightness(brightness, self._color)
            self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
            print("EEE")
        else:
            await self._device.turn_on()
        self._attr_is_on = True
        self._attr_available = True
        self.schedule_update_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Instruct the light to turn off."""
        print("TURNOFF")
        # TODO handle error and availability False
        await self._device.turn_off()
        self._attr_is_on = False
        self._attr_brightness = 0
        print(self._attr_is_on)
        print("TURNOFF2")
        self._attr_available = True
        self.schedule_update_ha_state()
