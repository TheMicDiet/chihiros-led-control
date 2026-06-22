"""Dosing pump button controls."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import async_trigger_dose_ml
from .const import DOMAIN
from .entity import chihiros_device_info, chihiros_entity_name, chihiros_unique_id
from .models import ChihirosData
from .runtime import ChihirosClient


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up dosing pump buttons."""
    chihiros_data: ChihirosData = hass.data[DOMAIN][entry.entry_id]
    if not chihiros_data.dosing_totals:
        return

    async_add_entities(
        ChihirosDosingButton(chihiros_data.device, chihiros_data, pump_idx)
        for pump_idx in range(chihiros_data.dosing_totals.pump_count)
    )


class ChihirosDosingButton(ButtonEntity):
    """Button entity for a one-shot manual dose."""

    _attr_should_poll = False

    def __init__(self, device: ChihirosClient, chihiros_data: ChihirosData, pump_idx: int) -> None:
        """Initialize the dosing button."""
        self._device = device
        self._chihiros_data = chihiros_data
        self._pump_idx = pump_idx
        pump_number = pump_idx + 1
        self._attr_name = chihiros_entity_name(device, f"Dose pump {pump_number}")
        self._attr_unique_id = chihiros_unique_id(device.address, f"dosing_pump_{pump_number}_dose")
        self._attr_device_info = chihiros_device_info(device, device.address)

    async def async_press(self) -> None:
        """Trigger a manual dose using this pump's configured volume."""
        await async_trigger_dose_ml(
            self._chihiros_data, self._pump_idx, self._chihiros_data.dosing_volumes[self._pump_idx]
        )
