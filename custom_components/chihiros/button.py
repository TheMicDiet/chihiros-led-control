from __future__ import annotations

import asyncio  # NEW: brief pause before triggering sensor refresh
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send  # NEW: notify sensors to refresh

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    coord = data.coordinator

    # Only for doser devices
    if getattr(coord, "device_type", "led") != "doser":
        return

    # Build from explicit enabled channels (Options) or fall back to 1..channel_count
    channels = list(getattr(coord, "enabled_channels", []))
    if not channels:
        count = int(getattr(coord, "channel_count", 4))
        channels = list(range(1, count + 1))
    entities = [DoserDoseNowButton(hass, entry, coord, ch) for ch in channels]
    async_add_entities(entities)


class DoserDoseNowButton(ButtonEntity):
    """Per-channel 'Dose now' button that calls the chihiros.dose_ml service
    and then requests an immediate totals refresh from the sensor platform.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:play-circle"

    def __init__(self, hass, entry, coord, ch: int) -> None:
        self._hass = hass
        self._entry = entry
        self._coord = coord
        self._ch = ch

        self._attr_name = f"Ch {ch} Dose Now"
        self._attr_unique_id = f"{coord.address}-ch{ch}-dose-now"
        # Separate device tile for the doser (distinct from the LED device tile)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            manufacturer="Chihiros",
            model="Doser",
            name=(self._entry.title or "Chihiros Doser"),
        )

    async def async_press(self) -> None:
        amount = getattr(self._coord, "doser_amounts", {}).get(self._ch, 1.0)

        # Send the dose via the integration's service
        await self._hass.services.async_call(
            DOMAIN,
            "dose_ml",
            {"address": self._coord.address, "channel": self._ch, "ml": float(amount)},
            blocking=True,
        )

        # NEW: Give the firmware a brief moment to update its internal totals,
        # then ask the sensor coordinator to refresh immediately via dispatcher.
        # The matching listener is set up in sensor.py and will call
        # coordinator.async_request_refresh() when it receives this signal.
        await asyncio.sleep(0.3)
        async_dispatcher_send(self._hass, f"{DOMAIN}_{self._entry.entry_id}_refresh_totals")
