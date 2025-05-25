"""Switch platform for Chihiros LED Control to toggle auto/manual mode."""

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.entity import DeviceInfo

from .chihiros_led_control.device import BaseDevice
from .const import DOMAIN, MANUFACTURER
from .coordinator import ChihirosDataUpdateCoordinator
from .models import ChihirosData

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the switch platform for Chihiros LED Control."""
    chihiros_data: ChihirosData = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            ChihirosAutoManualSwitch(
                chihiros_data.coordinator, chihiros_data.device, entry
            )
        ]
    )


class ChihirosAutoManualSwitch(SwitchEntity):
    """Switch to toggle between auto and manual mode."""

    def __init__(
        self,
        coordinator: ChihirosDataUpdateCoordinator,
        device: BaseDevice,
        config_entry,
    ):
        """Initialize the switch."""
        self._device = device
        self._coordinator = coordinator
        self._attr_name = f"{device.name} Auto Mode"
        self._attr_unique_id = f"{coordinator.address}_auto_mode"
        self._attr_is_on = False
        self._attr_device_info = DeviceInfo(
            connections={("bluetooth", coordinator.address)},
            manufacturer=MANUFACTURER,
            model=device.model_name,
            name=device.name,
        )

    @property
    def is_on(self):
        """Return True if the switch is in auto mode."""
        return self._attr_is_on

    async def async_turn_on(self, **kwargs):
        """Auto mode: set brightness to auto level and enable auto mode."""
        await self._device.enable_auto_mode()
        self._attr_is_on = True
        self.async_write_ha_state()
        _LOGGER.debug("Switched to auto mode for %s", self._device.name)

    async def async_turn_off(self, **kwargs):
        """Manual mode: set brightness to last known or default value."""
        await self._device.set_manual_mode()
        self._attr_is_on = False
        self.async_write_ha_state()
        _LOGGER.debug("Switched to manual mode for %s", self._device.name)
