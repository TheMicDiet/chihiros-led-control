from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Optional

from homeassistant.components import bluetooth
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, CoordinatorEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BLEAK_RETRY_EXCEPTIONS as BLEAK_EXC,
    establish_connection,
)

from .const import DOMAIN
from .chihiros_doser_control.protocol import UART_TX  # notify UUID

_LOGGER = logging.getLogger(__name__)

SCAN_TIMEOUT = 3.0            # seconds to wait for one notify
UPDATE_EVERY = timedelta(minutes=15)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    address: Optional[str] = getattr(getattr(data, "coordinator", None), "address", None)
    _LOGGER.debug("chihiros.sensor: setup entry=%s addr=%s", entry.entry_id, address)

    coordinator = DoserTotalsCoordinator(hass, address, entry)

    # Do NOT block platform setup on BLE; schedule a refresh instead
    hass.async_create_task(coordinator.async_request_refresh())

    # Listen for “Dose Now” to force an immediate refresh
    signal = f"{DOMAIN}_{entry.entry_id}_refresh_totals"
    unsub = async_dispatcher_connect(
        hass,
        signal,
        lambda: hass.async_create_task(coordinator.async_request_refresh()),
    )
    entry.async_on_unload(unsub)

    sensors = [ChDoserDailyTotalSensor(coordinator, entry, ch) for ch in range(4)]
    async_add_entities(sensors, update_before_add=False)


class DoserTotalsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Listen briefly for the 0x5B/0x22 totals notify and decode hi/lo pairs."""

    def __init__(self, hass: HomeAssistant, address: Optional[str], entry: ConfigEntry):
        super().__init__(hass, logger=_LOGGER, name=f"{DOMAIN}-doser-totals", update_interval=UPDATE_EVERY)
        self.address = address
        self.entry = entry
        self._last: dict[str, Any] = {"ml": [None, None, None, None], "raw": None}
        self._lock = asyncio.Lock()  # avoid overlapping BLE connects

    async def _async_update_data(self) -> dict[str, Any]:
        async with self._lock:
            if not self.address:
                _LOGGER.debug("sensor: no BLE address; keeping last values")
                return self._last

            ble_dev = bluetooth.async_ble_device_from_address(self.hass, self.address, True)
            if not ble_dev:
                _LOGGER.debug("sensor: no BLEDevice for %s; keeping last", self.address)
                return self._last

            fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

            def _cb(_char, payload: bytearray):
                try:
                    if len(payload) < 8:
                        return
                    cmd = payload[0]
                    mode = payload[5] if len(payload) >= 6 else None
                    params = list(payload[6:-1]) if len(payload) >= 8 else []
                    _LOGGER.debug("sensor: notify cmd=0x%02X mode=%s params=%s",
                                  cmd, f"0x{mode:02X}" if isinstance(mode, int) else None, params)
                    # Expect 8 params: (hi0,lo0, hi1,lo1, hi2,lo2, hi3,lo3)
                    if cmd in (0x5B, 91) and mode == 0x22 and len(params) == 8:
                        pairs = list(zip(params[0::2], params[1::2]))
                        ml = [round(h * 25.6 + l / 10.0, 1) for h, l in pairs]
                        if not fut.done():
                            fut.set_result({"ml": ml, "raw": bytes(payload)})
                except Exception:
                    _LOGGER.exception("sensor: notify parse error")

            client = None
            try:
                # Use HA-friendly connector; it queues if a slot isn’t available
                client = await establish_connection(
                    BleakClientWithServiceCache, ble_dev, f"{DOMAIN}-totals"
                )
                await client.start_notify(UART_TX, _cb)
                try:
                    res = await asyncio.wait_for(fut, timeout=SCAN_TIMEOUT)
                    self._last = res
                except asyncio.TimeoutError:
                    _LOGGER.debug("sensor: no totals frame within %.1fs; keeping last", SCAN_TIMEOUT)
                finally:
                    try:
                        await client.stop_notify(UART_TX)
                    except Exception:
                        pass
            except BLEAK_EXC as e:
                _LOGGER.debug("sensor: BLE/slot error: %s; keeping last", e)
            except Exception as e:
                _LOGGER.warning("sensor: BLE error: %s", e)
            finally:
                if client:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass

            return self._last


class ChDoserDailyTotalSensor(CoordinatorEntity[DoserTotalsCoordinator], SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "mL"
    _attr_has_entity_name = True

    def __init__(self, coordinator: DoserTotalsCoordinator, entry: ConfigEntry, ch: int):
        super().__init__(coordinator)
        self._ch = ch
        self._attr_name = f"Ch {ch + 1} Daily Dose"
        self._attr_unique_id = f"{entry.entry_id}-doser-ch{ch+1}-daily_total_ml"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def native_value(self) -> Optional[float]:
        ml = (self.coordinator.data or {}).get("ml") or [None, None, None, None]
        return ml[self._ch] if self._ch < len(ml) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        raw = (self.coordinator.data or {}).get("raw")
        return {"raw_frame": raw.hex(" ").upper() if isinstance(raw, (bytes, bytearray)) else None}
