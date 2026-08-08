"""Dosing pump and fan number controls."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .entity import chihiros_device_info, chihiros_entity_name, chihiros_unique_id
from .models import ChihirosData
from .runtime import ChihirosClient

_LOGGER = logging.getLogger(__name__)

# Auto mode keeps at least this gap between the fan start and stop temperatures,
# matching the vendor app's hysteresis handling for temperature-driven fans.
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
    entities: list[NumberEntity] = []

    if chihiros_data.dosing_totals:
        entities.extend(
            ChihirosDosingVolumeNumber(chihiros_data.device, chihiros_data, pump_idx)
            for pump_idx in range(chihiros_data.dosing_totals.pump_count)
        )

    if chihiros_data.device.model.has_fan:
        entities.extend(
            (
                ChihirosFanStartTempNumber(chihiros_data.device),
                ChihirosFanStopTempNumber(chihiros_data.device),
            )
        )

    if entities:
        async_add_entities(entities)


class ChihirosDosingVolumeNumber(NumberEntity, RestoreEntity):
    """Number entity for a pump's manual dose volume."""

    _attr_should_poll = False
    _attr_native_min_value = 0.2
    _attr_native_max_value = 999.9
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = UnitOfVolume.MILLILITERS
    _attr_mode = NumberMode.BOX

    def __init__(self, device: ChihirosClient, chihiros_data: ChihirosData, pump_idx: int) -> None:
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


class ChihirosFanTempNumberBase(NumberEntity, RestoreEntity):
    """Base for the VIVID3 fan start/stop temperature numbers."""

    _attr_should_poll = False
    _attr_native_min_value = 15
    _attr_native_max_value = 60
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_mode = NumberMode.BOX

    def __init__(self, device: ChihirosClient) -> None:
        """Initialize the fan temperature number."""
        self._device = device
        self._restored_value: float | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the configured value and refresh with paired number changes."""
        await super().async_added_to_hass()
        if last_state := await self.async_get_last_state():
            try:
                value = float(last_state.state)
            except ValueError:
                value = None
            if value is not None and self.native_min_value <= value <= self.native_max_value:
                pair = self._restored_temperature_pair(int(value))
                try:
                    await self._device.set_fan_start_stop_temp(*pair)
                    self._restored_value = float(self._restored_value_from_pair(pair))
                except Exception:
                    _LOGGER.debug("Failed to restore fan temperature for %s", self.name, exc_info=True)
        self.async_on_remove(
            async_dispatcher_connect(self.hass, _fan_temp_signal(self._device.address), self._async_temp_updated)
        )

    @callback
    def _async_temp_updated(self) -> None:
        """Use the device-side pair after either temperature is changed."""
        self._restored_value = None
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        """Return the configured fan temperature."""
        if self._restored_value is not None:
            return self._restored_value
        return self._current_value()

    def _current_value(self) -> float:
        """Return this number's device-side value."""
        raise NotImplementedError

    def _restored_temperature_pair(self, value: int) -> tuple[int, int]:
        """Return the complete fan temperature pair for a restored value."""
        raise NotImplementedError

    def _restored_value_from_pair(self, pair: tuple[int, int]) -> int:
        """Return this entity's value from a complete restored pair."""
        raise NotImplementedError

    async def _apply_temps(self, start_temp: int, stop_temp: int) -> None:
        """Persist both fan temperatures on the device and notify the paired number."""
        try:
            await self._device.set_fan_start_stop_temp(start_temp, stop_temp)
        except Exception as ex:
            raise HomeAssistantError(f"Failed to set fan temperature for {self.name}") from ex
        self._restored_value = None
        async_dispatcher_send(self.hass, _fan_temp_signal(self._device.address))


class ChihirosFanStartTempNumber(ChihirosFanTempNumberBase):
    """Number entity for the VIVID3 fan auto-mode start temperature."""

    def __init__(self, device: ChihirosClient) -> None:
        """Initialize the fan start temperature number."""
        super().__init__(device)
        self._attr_name = chihiros_entity_name(device, "Fan start temp")
        self._attr_unique_id = chihiros_unique_id(device.address, "fan_start_temp")
        self._attr_device_info = chihiros_device_info(device, device.address)

    def _current_value(self) -> float:
        """Return the fan start temperature stored on the device client."""
        return float(self._device.fan_start_temp)

    def _restored_temperature_pair(self, value: int) -> tuple[int, int]:
        """Return restored start temperature with a valid stop temperature."""
        stop_temp = min(self._device.fan_stop_temp, value - FAN_TEMP_HYSTERESIS)
        return max(self._attr_native_min_value + FAN_TEMP_HYSTERESIS, value), max(
            self._attr_native_min_value, stop_temp
        )

    def _restored_value_from_pair(self, pair: tuple[int, int]) -> int:
        """Return the restored start temperature."""
        return pair[0]

    async def async_set_native_value(self, value: float) -> None:
        """Set the fan start temperature, keeping the stop temperature below it."""
        start_temp = max(int(value), self._attr_native_min_value + FAN_TEMP_HYSTERESIS)
        stop_temp = min(self._device.fan_stop_temp, start_temp - FAN_TEMP_HYSTERESIS)
        stop_temp = max(self._attr_native_min_value, stop_temp)
        await self._apply_temps(start_temp, stop_temp)


class ChihirosFanStopTempNumber(ChihirosFanTempNumberBase):
    """Number entity for the VIVID3 fan auto-mode stop temperature."""

    def __init__(self, device: ChihirosClient) -> None:
        """Initialize the fan stop temperature number."""
        super().__init__(device)
        self._attr_name = chihiros_entity_name(device, "Fan stop temp")
        self._attr_unique_id = chihiros_unique_id(device.address, "fan_stop_temp")
        self._attr_device_info = chihiros_device_info(device, device.address)

    def _current_value(self) -> float:
        """Return the fan stop temperature stored on the device client."""
        return float(self._device.fan_stop_temp)

    def _restored_temperature_pair(self, value: int) -> tuple[int, int]:
        """Return restored stop temperature with a valid start temperature."""
        stop_temp = min(self._attr_native_max_value - FAN_TEMP_HYSTERESIS, value)
        start_temp = max(self._device.fan_start_temp, stop_temp + FAN_TEMP_HYSTERESIS)
        return min(self._attr_native_max_value, start_temp), stop_temp

    def _restored_value_from_pair(self, pair: tuple[int, int]) -> int:
        """Return the restored stop temperature."""
        return pair[1]

    async def async_set_native_value(self, value: float) -> None:
        """Set the fan stop temperature, keeping the start temperature above it."""
        stop_temp = min(int(value), self._attr_native_max_value - FAN_TEMP_HYSTERESIS)
        start_temp = max(self._device.fan_start_temp, stop_temp + FAN_TEMP_HYSTERESIS)
        start_temp = min(self._attr_native_max_value, start_temp)
        await self._apply_temps(start_temp, stop_temp)
