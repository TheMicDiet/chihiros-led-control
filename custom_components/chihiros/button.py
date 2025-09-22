from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    coord = data.coordinator

    # Only create these for doser devices
    if getattr(coord, "device_type", "led") != "doser":
        return

    entities = [DoserDoseNowButton(hass, entry, coord, ch) for ch in range(1, 5)]
    async_add_entities(entities)


class DoserDoseNowButton(ButtonEntity):
    """Per-channel 'Dose now' button that calls the chihiros.dose_ml service."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:play-circle"

    def __init__(self, hass, entry, coord, ch: int) -> None:
        self._hass = hass
        self._entry = entry
        self._coord = coord
        self._ch = ch

        self._attr_name = f"Ch {ch} Dose Now"
        self._attr_unique_id = f"{coord.address}-ch{ch}-dose-now"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._entry.unique_id or coord.address)},
            manufacturer="Chihiros",
            model="Doser",
            name=getattr(coord, "name", "Chihiros Doser"),
        )

    async def async_press(self) -> None:
        # Pull currently set amount from the number entity / coordinator (default 1.0 mL)
        amount = getattr(self._coord, "doser_amounts", {}).get(self._ch, 1.0)

        # Use address directly so we don't have to chase device_id here
        await self._hass.services.async_call(
            DOMAIN,
            "dose_ml",
            {"address": self._coord.address, "channel": self._ch, "ml": float(amount)},
            blocking=True,
        )
