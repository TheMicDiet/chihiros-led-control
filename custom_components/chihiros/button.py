"""Dosing pump and heater button controls."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .dosing_services import async_trigger_dose_ml
from .entity import chihiros_device_info, chihiros_entity_name, chihiros_unique_id
from .heater import ChihirosHeaterResetWorkTimeButton
from .models import ChihirosData, DosingChihirosData
from .runtime import DosingChihirosClient, is_device_kind
from .vendor.chihiros_led_control.models import DeviceKind


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up dosing pump and heater buttons."""
    chihiros_data: ChihirosData = hass.data[DOMAIN][entry.entry_id]
    entities: list[ButtonEntity] = []
    if isinstance(chihiros_data, DosingChihirosData):
        entities.extend(
            ChihirosDosingButton(chihiros_data.device, chihiros_data, pump_idx)
            for pump_idx in range(chihiros_data.dosing_totals.pump_count)
        )
        entities.append(ChihirosCalibrationButton(chihiros_data))
    if is_device_kind(chihiros_data.device, DeviceKind.HEATER):
        entities.append(ChihirosHeaterResetWorkTimeButton(chihiros_data.coordinator, chihiros_data.device))
    if entities:
        async_add_entities(entities)


class ChihirosDosingButton(ButtonEntity):
    """Button entity for a one-shot manual dose."""

    _attr_should_poll = False

    def __init__(self, device: DosingChihirosClient, chihiros_data: DosingChihirosData, pump_idx: int) -> None:
        """Initialize the dosing button."""
        self._device = device
        self._chihiros_data = chihiros_data
        self._pump_idx = pump_idx
        pump_number = pump_idx + 1
        self._attr_name = chihiros_entity_name(device, f"Pump {pump_number} dose")
        self._attr_unique_id = chihiros_unique_id(device.address, f"dosing_pump_{pump_number}_dose")
        self._attr_device_info = chihiros_device_info(device, device.address)

    async def async_press(self) -> None:
        """Trigger a manual dose using this pump's configured volume."""
        await async_trigger_dose_ml(
            self.hass, self._chihiros_data, self._pump_idx, self._chihiros_data.dosing_volumes[self._pump_idx]
        )


class ChihirosCalibrationButton(ButtonEntity):
    """Button entity that opens the dosing pump calibration wizard."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, chihiros_data: DosingChihirosData) -> None:
        """Initialize the calibration wizard button."""
        self._chihiros_data = chihiros_data
        self._attr_name = chihiros_entity_name(chihiros_data.device, "Calibrate pump")
        self._attr_unique_id = chihiros_unique_id(chihiros_data.device.address, "dosing_pump_calibrate")
        self._attr_device_info = chihiros_device_info(chihiros_data.device, chihiros_data.device.address)

    async def async_press(self) -> None:
        """Start the per-channel calibration wizard."""
        from .calibration_flow import async_start_calibration_issue

        async_start_calibration_issue(self.hass, self._chihiros_data)
