"""Dosing pump and fan number controls."""

from __future__ import annotations

import asyncio
import logging
from typing import cast

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.restore_state import async_get as async_get_restore_data

from .const import DOMAIN
from .entity import chihiros_device_info, chihiros_entity_name, chihiros_unique_id
from .heater import (
    ChihirosHeaterCalibrationNumber,
    ChihirosHeaterPowerNumber,
    ChihirosHeaterProtectorNumber,
    ChihirosHeaterTemperatureNumber,
    heater_client,
    is_heater_capable,
)
from .models import ChihirosData, DosingChihirosData, StirrerChihirosData
from .runtime import DosingChihirosClient, LedChihirosClient, has_led_feature
from .stirrer import ChihirosStirPreRunNumber, ChihirosStirSpeedNumber
from .vendor.chihiros_led_control.models import LedFeature
from .vendor.chihiros_led_control.protocol.dosing import MANUAL_DOSE_VOLUME_MAX_ML, MANUAL_DOSE_VOLUME_MIN_ML

_LOGGER = logging.getLogger(__name__)

# Minimum gap between the fan start and stop temperatures (vendor app hysteresis).
FAN_TEMP_HYSTERESIS = 2
SIGNAL_FAN_TEMP_UPDATED = f"{DOMAIN}_fan_temp_updated"


def _fan_temp_signal(address: str) -> str:
    """Return the dispatcher signal for one device's fan temperature controls."""
    return f"{SIGNAL_FAN_TEMP_UPDATED}_{address.lower()}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number controls for the configured Chihiros device."""
    chihiros_data: ChihirosData = hass.data[DOMAIN][entry.entry_id]
    _remove_obsolete_heater_numbers(hass, entry, chihiros_data)
    entities: list[NumberEntity] = []
    if isinstance(chihiros_data, DosingChihirosData):
        entities.extend(
            ChihirosDosingVolumeNumber(chihiros_data.device, chihiros_data, pump_idx)
            for pump_idx in range(chihiros_data.dosing_totals.pump_count)
        )
    if has_led_feature(chihiros_data.device, LedFeature.FAN):
        device = cast(LedChihirosClient, chihiros_data.device)
        pair = FanTemperaturePair()
        entities.extend((ChihirosFanStartTempNumber(device, pair), ChihirosFanStopTempNumber(device, pair)))
    entities.extend(_stirrer_numbers(chihiros_data))
    entities.extend(_heater_numbers(chihiros_data))
    if entities:
        async_add_entities(entities)


def _remove_obsolete_heater_numbers(hass: HomeAssistant, entry: ConfigEntry, data: ChihirosData) -> None:
    """Remove the current heater's obsolete separate auto-setpoint entities."""
    if not is_heater_capable(data.device):
        return
    registry = er.async_get(hass)
    for suffix in ("heater_auto_temperature", "heater_auto_power"):
        entity_id = registry.async_get_entity_id("number", DOMAIN, chihiros_unique_id(data.coordinator.address, suffix))
        if entity_id is not None and registry.async_get(entity_id).config_entry_id == entry.entry_id:
            registry.async_remove(entity_id)


def _heater_numbers(chihiros_data: ChihirosData) -> list[NumberEntity]:
    """Build the shared setpoints and configuration numbers for a heater."""
    if not is_heater_capable(chihiros_data.device):
        return []
    device = heater_client(chihiros_data.device)
    return [
        ChihirosHeaterTemperatureNumber(chihiros_data.coordinator, device),
        ChihirosHeaterPowerNumber(chihiros_data.coordinator, device),
        ChihirosHeaterProtectorNumber(chihiros_data.coordinator, device),
        ChihirosHeaterCalibrationNumber(chihiros_data.coordinator, device),
    ]


def _stirrer_numbers(chihiros_data: ChihirosData) -> list[NumberEntity]:
    """Build the speed and pre-run numbers for a magnetic stirrer."""
    if not isinstance(chihiros_data, StirrerChihirosData):
        return []
    return [
        entity
        for channel in range(len(chihiros_data.stirrer_states))
        for entity in (
            ChihirosStirSpeedNumber(chihiros_data.device, chihiros_data, channel),
            ChihirosStirPreRunNumber(chihiros_data.device, chihiros_data, channel),
        )
    ]


class ChihirosDosingVolumeNumber(NumberEntity, RestoreEntity):
    """Number entity for a pump's manual dose volume."""

    _attr_native_min_value = MANUAL_DOSE_VOLUME_MIN_ML
    _attr_native_max_value = MANUAL_DOSE_VOLUME_MAX_ML
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = UnitOfVolume.MILLILITERS
    _attr_mode = NumberMode.BOX

    def __init__(self, device: DosingChihirosClient, chihiros_data: DosingChihirosData, pump_idx: int) -> None:
        """Initialize the dose volume number."""
        self._device = device
        self._chihiros_data = chihiros_data
        self._pump_idx = pump_idx
        pump_number = pump_idx + 1
        self._attr_name = chihiros_entity_name(device, f"Pump {pump_number} dose volume")
        self._attr_unique_id = chihiros_unique_id(device.address, f"dosing_pump_{pump_number}_dose_volume")
        self._attr_device_info = chihiros_device_info(device, device.address)
        self._attr_native_value = chihiros_data.dosing_volumes[pump_idx]

    async def async_added_to_hass(self) -> None:
        """Restore the last configured manual dose volume."""
        if last_state := await self.async_get_last_state():
            try:
                value = round(float(last_state.state), 1)
            except ValueError:
                return
            if self.native_min_value <= value <= self.native_max_value:
                self._set_value(value)

    async def async_set_native_value(self, value: float) -> None:
        """Set the dose volume for this pump."""
        self._set_value(round(value, 1))
        self.async_write_ha_state()

    def _set_value(self, value: float) -> None:
        """Update local runtime state for this pump volume."""
        self._chihiros_data.dosing_volumes[self._pump_idx] = value
        self._attr_native_value = value


class FanTemperaturePair:
    """Share startup restoration and atomic temperature updates for one device."""

    def __init__(self) -> None:
        """Initialize the pair's command and lifecycle state."""
        self.lock = asyncio.Lock()
        self.revision = 0
        self.initialized = False
        self.active_entities: set[ChihirosFanTempNumberBase] = set()
        self.restored: tuple[int, int] | None = None
        self.restore_task: asyncio.Task[None] | None = None

    async def _restore_temperatures(self, hass: HomeAssistant, device: LedChihirosClient, revision: int) -> None:
        """Apply the shared snapshot without depending on any one entity's lifetime."""
        async with self.lock:
            if not self.active_entities or revision != self.revision:
                return
            if self.restored is None:
                return
            try:
                await device.set_fan_start_stop_temp(*self.restored)
            except Exception:
                _LOGGER.debug("Failed to restore fan temperature for %s", device.address, exc_info=True)
                return
            self.restored = None
            async_dispatcher_send(hass, _fan_temp_signal(device.address))


class ChihirosFanTempNumberBase(NumberEntity, RestoreEntity):
    """Base for the VIVID3 fan start/stop temperature numbers."""

    _attr_should_poll = False
    _attr_native_min_value = 15
    _attr_native_max_value = 60
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_mode = NumberMode.BOX

    _partner_unique_id_suffix = ""
    _is_start = False

    def __init__(self, device: LedChihirosClient, pair: FanTemperaturePair) -> None:
        """Initialize the fan temperature number with its shared pair state."""
        self._device = device
        self._pair = pair

    async def async_added_to_hass(self) -> None:
        """Snapshot the stored pair once and defer its device write."""
        await super().async_added_to_hass()
        self._pair.active_entities.add(self)
        last_state = await self.async_get_last_state()
        if not self._pair.initialized and self._pair.revision == 0:
            self._pair.initialized = True
            value = self._stored_value(last_state.state) if last_state is not None else None
            self._pair.restored = self._restored_temperature_pair(value, self._partner_restored_value())
        if (
            self._pair.restored is not None
            and self._pair.revision == 0
            and (self._pair.restore_task is None or self._pair.restore_task.done())
        ):
            self._pair.restore_task = self.hass.async_create_task(
                self._pair._restore_temperatures(self.hass, self._device, self._pair.revision)
            )
        self.async_on_remove(
            async_dispatcher_connect(self.hass, _fan_temp_signal(self._device.address), self._async_temp_updated)
        )

    async def async_will_remove_from_hass(self) -> None:
        """Cancel shared restoration only after the last active number leaves."""
        self._pair.active_entities.discard(self)
        if not self._pair.active_entities and self._pair.restore_task is not None:
            self._pair.restore_task.cancel()
            await asyncio.gather(self._pair.restore_task, return_exceptions=True)
        await super().async_will_remove_from_hass()

    @callback
    def _async_temp_updated(self) -> None:
        """Use the device-side pair after either temperature is changed."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        """Return the configured fan temperature."""
        if self._pair.restored is not None:
            return float(self._pair.restored[0 if self._is_start else 1])
        return self._current_value()

    def _current_value(self) -> float:
        """Return this number's device-side value."""
        raise NotImplementedError

    def _restored_temperature_pair(self, value: float | None, partner: float | None) -> tuple[int, int] | None:
        """Build one normalized startup snapshot, including a disabled partner."""
        if (value, partner) == (None, None):
            return None
        start, stop = (value, partner) if self._is_start else (partner, value)
        start_temp = int(start if start is not None else self._device.fan_start_temp)
        stop_temp = int(stop if stop is not None else self._device.fan_stop_temp)
        stop_temp = max(self._attr_native_min_value, min(stop_temp, self._attr_native_max_value - FAN_TEMP_HYSTERESIS))
        start_temp = min(self._attr_native_max_value, max(start_temp, stop_temp + FAN_TEMP_HYSTERESIS))
        return start_temp, stop_temp

    def _stored_value(self, state: str) -> float | None:
        """Parse a saved temperature only when it is inside the supported range."""
        try:
            value = float(state)
        except (TypeError, ValueError):
            return None
        if self._attr_native_min_value <= value <= self._attr_native_max_value:
            return value
        return None

    def _partner_restored_value(self) -> float | None:
        """Return the paired number's restored value from the restore store, if any."""
        partner_unique_id = chihiros_unique_id(self._device.address, self._partner_unique_id_suffix)
        registry = er.async_get(self.hass)
        partner_entity_id = registry.async_get_entity_id("number", DOMAIN, partner_unique_id)
        if partner_entity_id is None:
            return None
        stored_state = async_get_restore_data(self.hass).last_states.get(partner_entity_id)
        if stored_state is None:
            return None
        return self._stored_value(stored_state.state.state)

    def _temperature_pair(self, value: float) -> tuple[int, int]:
        """Derive this command's pair while holding the shared lock."""
        raise NotImplementedError

    async def async_set_native_value(self, value: float) -> None:
        """Read, derive, write and commit both temperatures as one operation."""
        async with self._pair.lock:
            start_temp, stop_temp = self._temperature_pair(value)
            try:
                await self._device.set_fan_start_stop_temp(start_temp, stop_temp)
            except Exception as ex:
                raise HomeAssistantError(f"Failed to set fan temperature for {self.name}") from ex
            self._pair.revision += 1
            self._pair.restored = None
            async_dispatcher_send(self.hass, _fan_temp_signal(self._device.address))


class ChihirosFanStartTempNumber(ChihirosFanTempNumberBase):
    """Number entity for the VIVID3 fan auto-mode start temperature."""

    _is_start = True

    def __init__(self, device: LedChihirosClient, pair: FanTemperaturePair) -> None:
        """Initialize the fan start temperature number."""
        super().__init__(device, pair)
        self._attr_name = chihiros_entity_name(device, "Fan start temp")
        self._attr_unique_id = chihiros_unique_id(device.address, "fan_start_temp")
        self._attr_device_info = chihiros_device_info(device, device.address)
        self._partner_unique_id_suffix = "fan_stop_temp"

    def _current_value(self) -> float:
        """Return the fan start temperature stored on the device client."""
        return float(self._device.fan_start_temp)

    def _temperature_pair(self, value: float) -> tuple[int, int]:
        """Set the start temperature, keeping the stop temperature below it."""
        start_temp = max(int(value), self._attr_native_min_value + FAN_TEMP_HYSTERESIS)
        stop_temp = min(self._device.fan_stop_temp, start_temp - FAN_TEMP_HYSTERESIS)
        stop_temp = max(self._attr_native_min_value, stop_temp)
        return start_temp, stop_temp


class ChihirosFanStopTempNumber(ChihirosFanTempNumberBase):
    """Number entity for the VIVID3 fan auto-mode stop temperature."""

    def __init__(self, device: LedChihirosClient, pair: FanTemperaturePair) -> None:
        """Initialize the fan stop temperature number."""
        super().__init__(device, pair)
        self._attr_name = chihiros_entity_name(device, "Fan stop temp")
        self._attr_unique_id = chihiros_unique_id(device.address, "fan_stop_temp")
        self._attr_device_info = chihiros_device_info(device, device.address)
        self._partner_unique_id_suffix = "fan_start_temp"

    def _current_value(self) -> float:
        """Return the fan stop temperature stored on the device client."""
        return float(self._device.fan_stop_temp)

    def _temperature_pair(self, value: float) -> tuple[int, int]:
        """Set the stop temperature, keeping the start temperature above it."""
        stop_temp = min(int(value), self._attr_native_max_value - FAN_TEMP_HYSTERESIS)
        start_temp = max(self._device.fan_start_temp, stop_temp + FAN_TEMP_HYSTERESIS)
        start_temp = min(self._attr_native_max_value, start_temp)
        return start_temp, stop_temp
