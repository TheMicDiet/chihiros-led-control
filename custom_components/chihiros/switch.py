"""Switch platform for Chihiros LED Control to toggle auto/manual mode."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import ChihirosDataUpdateCoordinator
from .entity import chihiros_device_info, chihiros_entity_name, chihiros_unique_id
from .heater import (
    ChihirosHeaterAutomaticPowerSwitch,
    ChihirosHeaterBacklightSwitch,
    heater_client,
    is_heater_capable,
)
from .models import ChihirosData, StirrerChihirosData
from .runtime import LedChihirosClient, has_led_feature, is_device_kind
from .stirrer import ChihirosStirSwitch
from .vendor.chihiros_led_control.models import DeviceKind, LedFeature

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the switch platform for Chihiros LED Control."""
    chihiros_data: ChihirosData = hass.data[DOMAIN][entry.entry_id]
    entities = _accessory_switches(chihiros_data)
    if not is_device_kind(chihiros_data.device, DeviceKind.LED):
        if entities:
            async_add_entities(entities)
        return
    device = cast(LedChihirosClient, chihiros_data.device)
    entities.append(
        ChihirosAutoManualSwitch(
            chihiros_data.coordinator,
            device,
        )
    )
    if has_led_feature(device, LedFeature.TEMPERATURE_PROTECTION):
        entities.extend(_vivid3_switches(device))
    async_add_entities(entities)


def _accessory_switches(chihiros_data: ChihirosData) -> list[SwitchEntity]:
    """Build the switches of the stirrer and heater accessories."""
    entities: list[SwitchEntity] = []
    if isinstance(chihiros_data, StirrerChihirosData):
        entities.extend(
            ChihirosStirSwitch(chihiros_data.device, chihiros_data, channel)
            for channel in range(len(chihiros_data.stirrer_states))
        )
    if is_heater_capable(chihiros_data.device):
        device = heater_client(chihiros_data.device)
        entities.extend(
            (
                ChihirosHeaterAutomaticPowerSwitch(chihiros_data.coordinator, device),
                ChihirosHeaterBacklightSwitch(chihiros_data.coordinator, device),
            )
        )
    return entities


def _vivid3_switches(device: LedChihirosClient) -> list[SwitchEntity]:
    """Build the VIVID III temperature-protection and indicator-LED switches."""
    return [
        ChihirosVivid3Switch(device, "temp_protect", device.set_temp_protect, "Temperature Protection"),
        ChihirosVivid3Switch(device, "bluetooth_led", device.set_bluetooth_led, "Indicator LED"),
    ]


class ChihirosAutoManualSwitch(
    PassiveBluetoothCoordinatorEntity[ChihirosDataUpdateCoordinator],
    SwitchEntity,
):
    """Switch to toggle between auto and manual mode."""

    def __init__(
        self,
        coordinator: ChihirosDataUpdateCoordinator,
        device: LedChihirosClient,
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
    def is_on(self) -> bool | None:
        """Return the tracked auto mode, or None if unknown."""
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

    The device sends no notification for these settings. A known value is restored
    without being re-sent; otherwise the switch stays unknown until a successful write.
    """

    _attr_should_poll = False

    def __init__(
        self,
        device: LedChihirosClient,
        unique_id_suffix: str,
        setter: Callable[[bool], Awaitable[None]],
        name_suffix: str,
    ) -> None:
        """Initialize the switch."""
        self._setter = setter
        self._attr_name = chihiros_entity_name(device, name_suffix)
        self._attr_unique_id = chihiros_unique_id(device.address, unique_id_suffix)
        self._attr_device_info = chihiros_device_info(device, device.address)

    async def async_added_to_hass(self) -> None:
        """Restore the last known state."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) and last_state.state in (STATE_ON, STATE_OFF):
            self._attr_is_on = last_state.state == STATE_ON

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the setting on the device."""
        await self._setter(True)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the setting on the device."""
        await self._setter(False)
        self._attr_is_on = False
        self.async_write_ha_state()
