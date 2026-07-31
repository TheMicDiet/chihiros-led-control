"""LED BLE integration light platform."""

from __future__ import annotations

import logging
import math
from typing import Any

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ATTR_RGBW_COLOR,
    ColorMode,
    LightEntity,
)
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
    channels = chihiros_data.device.colors
    has_rgb = "red" in channels and "green" in channels and "blue" in channels
    # RGB/WRGB devices expose a single unified entity that writes every colour
    # channel in one BLE transaction, so per-channel entities are not created for
    # them (per-channel writes collide on the wire). White-only and partial
    # colour models still get one brightness entity per channel.
    if not has_rgb:
        for color in channels:
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

    if has_rgb:
        _LOGGER.debug("Setup chihiros RGB light entity: %s", chihiros_data.device.address)
        async_add_entities(
            [
                ChihirosRGBLightEntity(
                    chihiros_data.coordinator,
                    chihiros_data.device,
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
        self.coordinator.async_set_auto_mode(False)


class ChihirosRGBLightEntity(
    PassiveBluetoothCoordinatorEntity[ChihirosDataUpdateCoordinator],
    LightEntity,
    RestoreEntity,
):
    """Unified RGB/RGBW light entity for a Chihiros device.

    Exposes a single entity that sets all color channels simultaneously
    in one BLE transaction, avoiding packet collisions that occur when
    per-channel entities are triggered in parallel.
    """

    _attr_assumed_state = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: ChihirosDataUpdateCoordinator,
        chihiros_device: ChihirosClient,
    ) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self._device = chihiros_device
        self._address = coordinator.address
        self._has_white = "white" in self._device.colors
        self._color_mode = ColorMode.RGBW if self._has_white else ColorMode.RGB
        self._attr_supported_color_modes = {self._color_mode}
        self._attr_color_mode = self._color_mode

        self._attr_name = chihiros_entity_name(self._device, "RGBW" if self._has_white else "RGB")
        self._attr_unique_id = chihiros_unique_id(self._address, "rgbw" if self._has_white else "rgb")
        self._attr_device_info = chihiros_device_info(self._device, self._address)

        self._attr_rgb_color: tuple[int, ...] = (255, 255, 255)
        if self._has_white:
            self._attr_rgbw_color: tuple[int, ...] | None = (255, 255, 255, 255)
        else:
            self._attr_rgbw_color = None

    async def async_added_to_hass(self) -> None:
        """Handle entity about to be added to hass event."""
        _LOGGER.debug("Called async_added_to_hass: %s", self.name)
        await super().async_added_to_hass()
        if last_state := await self.async_get_last_state():
            self._attr_is_on = last_state.state == STATE_ON
            self._attr_brightness = last_state.attributes.get("brightness")
            if rgb := last_state.attributes.get("rgb_color"):
                self._attr_rgb_color = tuple(rgb)
            if rgbw := last_state.attributes.get("rgbw_color"):
                self._attr_rgbw_color = tuple(rgbw)

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
    def rgb_color(self) -> tuple[int, ...] | None:
        """Return the rgb color."""
        return self._attr_rgb_color

    @property
    def rgbw_color(self) -> tuple[int, ...] | None:
        """Return the rgbw color."""
        return self._attr_rgbw_color

    @property
    def color_mode(self) -> str | None:
        """Return the color mode of the light."""
        return self._attr_color_mode

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Instruct the light to turn on."""
        master_brightness = kwargs.get(ATTR_BRIGHTNESS, self._attr_brightness or 255)
        self._attr_brightness = int(master_brightness)

        if ATTR_RGBW_COLOR in kwargs and self._has_white:
            r, g, b, w = (int(c) for c in kwargs[ATTR_RGBW_COLOR])
            self._attr_rgbw_color = (r, g, b, w)
            self._attr_rgb_color = (r, g, b)
            await self._set_rgb_brightness((r, g, b, w), self._attr_brightness)
        elif ATTR_RGB_COLOR in kwargs:
            # Home Assistant converts rgb_color -> rgbw_color (white=0) for
            # RGBW entities, so this branch is only reached on pure RGB models.
            r, g, b = (int(c) for c in kwargs[ATTR_RGB_COLOR])
            self._attr_rgb_color = (r, g, b)
            await self._set_rgb_brightness((r, g, b), self._attr_brightness)
        else:
            if self._has_white:
                color = self._attr_rgbw_color or (255, 255, 255, 255)
                await self._set_rgb_brightness(color, self._attr_brightness)
            else:
                color = self._attr_rgb_color or (255, 255, 255)
                await self._set_rgb_brightness(color, self._attr_brightness)

        self._attr_is_on = True
        self.schedule_update_ha_state()
        _LOGGER.debug("Turned on: %s", self.name)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Instruct the light to turn off."""
        _LOGGER.debug("Turning off: %s", self.name)
        channels = dict.fromkeys(self._device.colors, 0)
        try:
            await self._device.set_brightness(channels)
        except Exception as ex:
            self._attr_available = False
            self.schedule_update_ha_state()
            raise HomeAssistantError(f"Failed to turn off {self.name}") from ex
        self._attr_available = True
        self._attr_is_on = False
        self._attr_brightness = 0
        self.coordinator.async_set_auto_mode(False)
        self.schedule_update_ha_state()
        _LOGGER.debug("Turned off: %s", self.name)

    @staticmethod
    def _scale_channel(value: int, master_brightness: int) -> int:
        """Scale a 0-255 colour value by master brightness to a 0-100 device level."""
        return max(0, math.ceil((value * master_brightness) / 255 / 255 * 100))

    async def _set_rgb_brightness(
        self,
        color: tuple[int, ...],
        master_brightness: int,
    ) -> None:
        """Send all colour channels to the device in one BLE transaction."""
        channels: dict[str, int] = {}
        if len(color) == 4:
            r, g, b, w = color
            channels = {
                "red": self._scale_channel(r, master_brightness),
                "green": self._scale_channel(g, master_brightness),
                "blue": self._scale_channel(b, master_brightness),
                "white": self._scale_channel(w, master_brightness),
            }
        else:
            r, g, b = color
            channels = {
                "red": self._scale_channel(r, master_brightness),
                "green": self._scale_channel(g, master_brightness),
                "blue": self._scale_channel(b, master_brightness),
            }

        _LOGGER.debug("Setting RGB brightness on %s: %s", self.name, channels)
        try:
            await self._device.set_brightness(channels)
        except Exception as ex:
            self._attr_available = False
            self.schedule_update_ha_state()
            raise HomeAssistantError(f"Failed to set brightness for {self.name}") from ex
        self._attr_available = True
        self.coordinator.async_set_auto_mode(False)
