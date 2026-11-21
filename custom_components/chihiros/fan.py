"""Fan platform for fan-equipped Chihiros devices such as the WRGB VIVID III."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import ATTR_FAN_RPM, ChihirosDataUpdateCoordinator
from .entity import chihiros_device_info, chihiros_entity_name, chihiros_unique_id
from .models import ChihirosData
from .runtime import LedChihirosClient, has_led_feature
from .vendor.chihiros_led_control.models import LedFeature

_LOGGER = logging.getLogger(__name__)
ATTR_LAST_MANUAL_PERCENTAGE = "last_manual_percentage"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the fan platform for fan-equipped Chihiros devices."""
    chihiros_data: ChihirosData = hass.data[DOMAIN][entry.entry_id]
    if not has_led_feature(chihiros_data.device, LedFeature.FAN):
        return
    device = cast(LedChihirosClient, chihiros_data.device)
    async_add_entities(
        [
            ChihirosFanEntity(
                chihiros_data.coordinator,
                device,
            )
        ]
    )


class ChihirosFanEntity(
    PassiveBluetoothCoordinatorEntity[ChihirosDataUpdateCoordinator],
    FanEntity,
    RestoreEntity,
):
    """Representation of a Chihiros device fan with manual speed and a temperature auto preset."""

    _attr_assumed_state = True
    _attr_should_poll = False
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED | FanEntityFeature.PRESET_MODE | FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
    )
    _attr_preset_modes = ["Auto", "Manual"]

    def __init__(
        self,
        coordinator: ChihirosDataUpdateCoordinator,
        chihiros_device: LedChihirosClient,
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
        self._command_lock = asyncio.Lock()
        self._command_revision = 0
        self._removed = False
        self._restore_task: asyncio.Task[None] | None = None

    async def async_added_to_hass(self) -> None:
        """Handle entity about to be added to hass event."""
        await super().async_added_to_hass()
        self._removed = False
        if last_state := await self.async_get_last_state():
            self._restore_attributes(last_state)
        if self._attr_preset_mode == "Auto":
            self._restore_task = self.hass.async_create_task(self._restore_auto_mode(self._command_revision))

    def _restore_attributes(self, last_state: State) -> None:
        """Restore both visible fan state and the remembered manual speed."""
        self._attr_percentage = last_state.attributes.get("percentage") or 0
        self._last_manual_percentage = last_state.attributes.get(ATTR_LAST_MANUAL_PERCENTAGE) or self._attr_percentage
        preset = last_state.attributes.get("preset_mode")
        if preset in self._attr_preset_modes:
            self._attr_preset_mode = preset

    async def async_will_remove_from_hass(self) -> None:
        """Cancel startup work before the old runtime can be unloaded."""
        self._removed = True
        if self._restore_task is not None:
            self._restore_task.cancel()
            await asyncio.gather(self._restore_task, return_exceptions=True)
        await super().async_will_remove_from_hass()

    async def _restore_auto_mode(self, revision: int) -> None:
        """Re-apply Auto only if no later explicit command superseded it."""
        async with self._command_lock:
            if self._removed or revision != self._command_revision:
                return
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
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return measured RPM and the remembered nonzero manual speed."""
        attributes = {ATTR_LAST_MANUAL_PERCENTAGE: self._last_manual_percentage}
        fan_rpm = self.coordinator.data.get(ATTR_FAN_RPM)
        if fan_rpm is not None:
            attributes[ATTR_FAN_RPM] = fan_rpm
        return attributes

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the fan speed percentage and switch to manual mode."""
        _LOGGER.debug("Setting fan speed: %s to %s%%", self.name, percentage)
        applied_percentage = percentage
        minimum = self._device.model.min_fan_speed
        if 0 < applied_percentage < minimum:
            applied_percentage = minimum
        async with self._command_lock:
            await self._set_fan_speed(applied_percentage)
            self._command_revision += 1
            self._attr_percentage = applied_percentage
            if applied_percentage > 0:
                self._last_manual_percentage = applied_percentage
            self._attr_preset_mode = "Manual"
            self.schedule_update_ha_state()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Switch between temperature-controlled auto mode and manual speed."""
        _LOGGER.debug("Setting fan preset: %s to %s", self.name, preset_mode)
        async with self._command_lock:
            if preset_mode == "Auto":
                await self._set_auto_mode()
            else:
                # The percentage attribute is stale while auto mode is active.
                percentage = self._last_manual_percentage or self._attr_percentage or 100
                await self._set_fan_speed(percentage)
                self._attr_percentage = percentage
                self._last_manual_percentage = percentage
                self._attr_preset_mode = "Manual"
            self._command_revision += 1
            self.schedule_update_ha_state()

    async def _set_auto_mode(self) -> None:
        """Enable Auto and commit the preset only after a successful write."""
        try:
            await self._device.set_fan_auto()
        except Exception as ex:
            raise HomeAssistantError(f"Failed to enable fan auto mode for {self.name}") from ex
        self._attr_preset_mode = "Auto"

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
        """Turn off the fan, including when temperature-controlled mode is active."""
        del kwargs
        await self.async_set_percentage(0)

    async def _set_fan_speed(self, percentage: int) -> None:
        """Send the fan speed command and raise on BLE failure."""
        try:
            await self._device.set_fan_speed(percentage)
        except Exception as ex:
            raise HomeAssistantError(f"Failed to set fan speed for {self.name}") from ex
