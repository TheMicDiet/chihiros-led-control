"""Select platform for Chihiros accessory settings."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .heater import ChihirosHeaterModeSelect, ChihirosHeaterTemperatureUnitSelect, heater_client, is_heater_capable
from .models import ChihirosData


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the select platform for Chihiros devices."""
    chihiros_data: ChihirosData = hass.data[DOMAIN][entry.entry_id]
    entities: list[SelectEntity] = []
    if is_heater_capable(chihiros_data.device):
        device = heater_client(chihiros_data.device)
        entities.extend(
            (
                ChihirosHeaterModeSelect(chihiros_data.coordinator, device),
                ChihirosHeaterTemperatureUnitSelect(chihiros_data.coordinator, device),
            )
        )
    if entities:
        async_add_entities(entities)
