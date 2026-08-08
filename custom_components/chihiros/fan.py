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
    """Representation of a Chihiros device fan.

    The fan supports manual speed plus a temperature-controlled auto preset
    (vendor app ``autoFan``); the device starts/stops the fan from the
    configured start/stop temperatures in auto mode.
    """

    _attr_assumed_state = True
    _attr_should_poll = False
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED | FanEntityFeature.PRESET_MODE | FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
    )
    _attr_preset_modes = ["Auto", "Manual"]

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
        self._last_manual_percentage = 0
        self._attr_preset_mode = "Manual"

    async def async_added_to_hass(self) -> None:
        """Handle entity about to be added to hass event."""
        await super().async_added_to_hass()
        if last_state := await self.async_get_last_state():
            self._attr_percentage = last_state.attributes.get("percentage") or 0
            if self._attr_percentage > 0:
                self._last_manual_percentage = self._attr_percentage
            preset = last_state.attributes.get("preset_mode")
            if preset in self._attr_preset_modes:
                self._attr_preset_mode = preset
        if self._attr_preset_mode == "Auto":
            self.hass.async_create_task(self._restore_auto_mode())

    async def _restore_auto_mode(self) -> None:
        """Re-apply the restored auto mode to a newly created device client."""
        try:
            await self._device.set_fan_auto()
        except Exception:
            _LOGGER.debug("Failed to restore fan auto mode for %s", self.name, exc_info=True)

    @property
    def available(self) -> bool:
        """Return whether the fan is available."""
        if self.coordinator.always_available:
            return True
        return super().available

    @property
    def is_on(self) -> bool:
        """Return whether the fan is running or auto control is enabled."""
        return bool(self._attr_preset_mode == "Auto" or (self._attr_percentage and self._attr_percentage > 0))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the measured fan RPM reported by status notifications."""
        fan_rpm = self.coordinator.data.get(ATTR_FAN_RPM)
        if fan_rpm is None:
            return None
        return {ATTR_FAN_RPM: fan_rpm}

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the fan speed percentage (manual mode)."""
        _LOGGER.debug("Setting fan speed: %s to %s%%", self.name, percentage)
        applied_percentage = percentage
        minimum = self._device.model.min_fan_speed
        if 0 < applied_percentage < minimum:
            applied_percentage = minimum
        await self._set_fan_speed(applied_percentage)
        self._attr_percentage = applied_percentage
        if applied_percentage > 0:
            self._last_manual_percentage = applied_percentage
        self._attr_preset_mode = "Manual"
        self.schedule_update_ha_state()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Switch between temperature-controlled auto mode and manual speed."""
        _LOGGER.debug("Setting fan preset: %s to %s", self.name, preset_mode)
        if preset_mode == "Auto":
            try:
                await self._device.set_fan_auto()
            except Exception as ex:
                raise HomeAssistantError(f"Failed to enable fan auto mode for {self.name}") from ex
            self._attr_preset_mode = "Auto"
        else:
            # Manual: re-apply the last speed so the fan leaves auto control.
            await self._set_fan_speed(self._attr_percentage or 100)
            self._attr_preset_mode = "Manual"
        self.schedule_update_ha_state()

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn on the fan."""
        del kwargs
        if preset_mode is not None:
            await self.async_set_preset_mode(preset_mode)
            return
        if percentage is None:
            percentage = self._last_manual_percentage or 100
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
