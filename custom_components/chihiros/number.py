"""Dosing pump number controls."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .entity import chihiros_device_info, chihiros_entity_name, chihiros_unique_id
from .models import ChihirosData
from .runtime import ChihirosClient


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up dosing pump volume controls."""
    chihiros_data: ChihirosData = hass.data[DOMAIN][entry.entry_id]
    if not chihiros_data.dosing_totals:
        return

    async_add_entities(
        ChihirosDosingVolumeNumber(chihiros_data.device, chihiros_data, pump_idx)
        for pump_idx in range(chihiros_data.dosing_totals.pump_count)
    )


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
