from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Optional

from bleak import BleakClient
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    CoordinatorEntity,
)

from .const import DOMAIN
# Reuse the doser UART UUIDs from your protocol module
from .chihiros_doser_control.protocol import UART_TX

_LOGGER = logging.getLogger(__name__)

SCAN_TIMEOUT = 2.0  # seconds to wait for one notify
UPDATE_EVERY = timedelta(minutes=15)  # adjust as you like


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Set up Chihiros doser sensors from a config entry."""
    data_by_entry = hass.data.get(DOMAIN, {})
    data = data_by_entry.get(entry.entry_id)
    address: Optional[str] = None

    # Most of your code stores coordinator.address on the entry data
    if data and hasattr(data, "coordinator") and hasattr(data.coordinator, "address"):
        address = data.coordinator.address

    _LOGGER.debug("chihiros.sensor: setup for entry %s (addr=%s)", entry.entry_id, address)

    coordinator = DoserTotalsCoordinator(hass, address, entry=entry)
    await coordinator.async_config_entry_first_refresh()

    entities: list[SensorEntity] = [
        ChDoserDailyTotalSensor(coordinator, entry, ch_index=0),
        ChDoserDailyTotalSensor(coordinator, entry, ch_index=1),
        ChDoserDailyTotalSensor(coordinator, entry, ch_index=2),
        ChDoserDailyTotalSensor(coordinator, entry, ch_index=3),
    ]
    async_add_entities(entities, update_before_add=True)


class DoserTotalsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll/refresh the 'daily totals' by listening for one notify frame."""

    def __init__(self, hass: HomeAssistant, address: Optional[str], entry: ConfigEntry):
        super().__init__(hass, logger=_LOGGER, name=f"{DOMAIN}-doser-totals", update_interval=UPDATE_EVERY)
        self.address = address
        self.entry = entry
        self._last: dict[str, Any] = {"ml": [None, None, None, None], "raw": None}

    async def _async_update_data(self) -> dict[str, Any]:
        if not self.address:
            _LOGGER.warning("chihiros.sensor: no BLE address; keeping last values")
            return self._last

        got: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

        def handle_notify(_char, payload: bytearray):
            try:
                # Expect 0x5B .. mode 0x22 and 8 params: (hi0,lo0, hi1,lo1, hi2,lo2, hi3,lo3)
                if len(payload) < 8:
                    return
                cmd_id = payload[0]
                mode = payload[5] if len(payload) >= 6 else None
                params = list(payload[6:-1]) if len(payload) >= 8 else []
                _LOGGER.debug("chihiros.sensor: notify cmd=0x%02X mode=%s params=%s",
                              cmd_id, f"0x{mode:02X}" if isinstance(mode, int) else None, params)
                if cmd_id in (0x5B, 91) and mode == 0x22 and len(params) == 8:
                    pairs = list(zip(params[0::2], params[1::2]))
                    ml = [round(hi * 25.6 + lo / 10.0, 1) for hi, lo in pairs]
                    if not got.done():
                        got.set_result({"ml": ml, "raw": bytes(payload)})
            except Exception:
                _LOGGER.exception("chihiros.sensor: notify parse error")

        try:
            async with BleakClient(self.address, timeout=8.0) as client:
                await client.start_notify(UART_TX, handle_notify)
                try:
                    # Wait briefly for one push; if nothing arrives, keep the previous values.
                    result = await asyncio.wait_for(got, timeout=SCAN_TIMEOUT)
                    self._last = result
                    return result
                except asyncio.TimeoutError:
                    _LOGGER.debug("chihiros.sensor: no totals frame within %.1fs; keeping last", SCAN_TIMEOUT)
                    return self._last
                finally:
                    try:
                        await client.stop_notify(UART_TX)
                    except Exception:
                        pass
        except Exception as e:
            _LOGGER.warning("chihiros.sensor: BLE error: %s", e)
            # BLE error → keep last known values
            return self._last


class ChDoserDailyTotalSensor(CoordinatorEntity[DoserTotalsCoordinator], SensorEntity):
    """One sensor per channel: 'Ch N Daily Dose' (mL)."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "mL"
    _attr_has_entity_name = True

    def __init__(self, coordinator: DoserTotalsCoordinator, entry: ConfigEntry, ch_index: int):
        super().__init__(coordinator)
        self._ch = ch_index
        self._attr_name = f"Ch {ch_index + 1} Daily Dose"
        self._attr_unique_id = f"{entry.entry_id}-doser-ch{ch_index+1}-daily_total_ml"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
        )

    @property
    def native_value(self) -> Optional[float]:
        ml_list = (self.coordinator.data or {}).get("ml") or [None, None, None, None]
        return ml_list[self._ch] if self._ch < len(ml_list) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        raw = (self.coordinator.data or {}).get("raw")
        return {"raw_frame": raw.hex(" ").upper() if isinstance(raw, (bytes, bytearray)) else None}
