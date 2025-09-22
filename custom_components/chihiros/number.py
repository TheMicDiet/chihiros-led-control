from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    coord = data.coordinator

    # Only for doser devices
    if getattr(coord, "device_type", "led") != "doser":
        return

    if not hasattr(coord, "doser_amounts"):
        coord.doser_amounts = {}

    # Build from explicit enabled channels (Options) or fall back to 1..channel_count
    channels = list(getattr(coord, "enabled_channels", []))
    if not channels:
        count = int(getattr(coord, "channel_count", 4))
        channels = list(range(1, count + 1))
    entities = [DoserDoseAmount(entry, coord, ch) for ch in channels]
    async_add_entities(entities)


class DoserDoseAmount(NumberEntity):
    """Per-channel 'Dose amount (mL)'."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:cup-water"
    _attr_native_min_value = 0.2
    _attr_native_max_value = 999.9
    _attr_native_step = 0.1
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "mL"

    def __init__(self, entry, coord, ch: int) -> None:
        self._entry = entry
        self._coord = coord
        self._ch = ch
        self._coord.doser_amounts.setdefault(self._ch, 1.0)

        self._attr_name = f"Ch {ch} Dose Amount"
        self._attr_unique_id = f"{coord.address}-ch{ch}-dose-amount"
        # Separate device tile for the doser (distinct from the LED device tile)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            manufacturer="Chihiros",
            model="Doser",
            name=(self._entry.title or "Chihiros Doser"),
        )

    @property
    def native_value(self) -> float:
        return float(self._coord.doser_amounts.get(self._ch, 1.0))

    async def async_set_native_value(self, value: float) -> None:
        v = max(self._attr_native_min_value, min(float(value), self._attr_native_max_value))
        self._coord.doser_amounts[self._ch] = round(v, 1)
        self.async_write_ha_state()
