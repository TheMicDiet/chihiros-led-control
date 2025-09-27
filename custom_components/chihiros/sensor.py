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
from .chihiros_doser_control import protocol as dp    # NEW: to build a query frame

_LOGGER = logging.getLogger(__name__)

SCAN_TIMEOUT = 8.0            # seconds to wait for one notify (slightly longer)
UPDATE_EVERY = timedelta(minutes=15)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    address: Optional[str] = getattr(getattr(data, "coordinator", None), "address", None)
    _LOGGER.debug("chihiros.sensor: setup entry=%s addr=%s", entry.entry_id, address)

    coordinator = DoserTotalsCoordinator(hass, address, entry)

    # Non-blocking initial refresh
    hass.async_create_task(coordinator.async_request_refresh())

    # Thread-safe dispatcher: refresh immediately after "Dose Now" (per-entry)
    signal = f"{DOMAIN}_{entry.entry_id}_refresh_totals"
    def _signal_refresh_entry() -> None:
        asyncio.run_coroutine_threadsafe(coordinator.async_request_refresh(), hass.loop)
    unsub = async_dispatcher_connect(hass, signal, _signal_refresh_entry)
    entry.async_on_unload(unsub)

    # NEW: also listen for per-address refresh requests
    if address:
        sig_addr = f"{DOMAIN}_refresh_totals_{address.lower()}"
        def _signal_refresh_addr() -> None:
            asyncio.run_coroutine_threadsafe(coordinator.async_request_refresh(), hass.loop)
        unsub2 = async_dispatcher_connect(hass, sig_addr, _signal_refresh_addr)
        entry.async_on_unload(unsub2)

    # NEW: push path — when the dose service emits decoded totals, adopt them
    if address:
        push_sig = f"{DOMAIN}_push_totals_{address.lower()}"
        def _on_push(data: dict[str, Any]) -> None:
            # Expect {"ml":[...], "raw": bytes/bytearray}
            coordinator.async_set_updated_data(data)
        unsub_push = async_dispatcher_connect(hass, push_sig, _on_push)
        entry.async_on_unload(unsub_push)

    sensors = [ChDoserDailyTotalSensor(coordinator, entry, ch) for ch in range(4)]
    async_add_entities(sensors, update_before_add=False)


class DoserTotalsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Listen briefly for the 0x5B totals notify and decode hi/lo pairs (modes vary)."""

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

            # IMPORTANT: HA stores addresses uppercase
            ble_dev = bluetooth.async_ble_device_from_address(self.hass, self.address.upper(), True)
            if not ble_dev:
                _LOGGER.debug("sensor: no BLEDevice for %s; keeping last", self.address)
                return self._last

            fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

            def _cb(_char, payload: bytearray):
                try:
                    if len(payload) < 8:
                        return

                    # NEW: use tolerant decoder if available; else fallback logic
                    values: Optional[list[float]] = None
                    if hasattr(dp, "parse_totals_frame"):
                        try:
                            values = dp.parse_totals_frame(payload)  # tolerant (any 0x5B mode, 8 params)
                        except Exception:
                            values = None

                    if values is None:
                        # Fallback: accept cmd=0x5B and at least 8 params after mode; use first 8
                        cmd = payload[0]
                        params = list(payload[6:-1]) if len(payload) >= 8 else []
                        if cmd in (0x5B, 91) and len(params) >= 8:
                            p8 = params[:8]
                            pairs = list(zip(p8[0::2], p8[1::2]))
                            values = [round(h * 25.6 + l / 10.0, 1) for h, l in pairs]

                    if values is not None and not fut.done():
                        fut.set_result({"ml": values, "raw": bytes(payload)})

                except Exception:
                    _LOGGER.exception("sensor: notify parse error")

            client = None
            try:
                # Use HA-friendly connector; it queues if a slot isn’t available
                client = await establish_connection(
                    BleakClientWithServiceCache, ble_dev, f"{DOMAIN}-totals"
                )
                await client.start_notify(UART_TX, _cb)

                # NEW: actively try a small set of probe frames to trigger totals push
                frames: list[bytes] = []
                try:
                    # Prefer helper from protocol.py if present
                    if hasattr(dp, "build_totals_probes"):
                        frames = list(dp.build_totals_probes())
                except Exception:
                    frames = []

                if not frames:
                    # Fallback set: 5B/0x22, 5B/0x1E, A5/0x22, A5/0x1E
                    try:
                        frames.extend([dp.encode_5b(0x22, []), dp.encode_5b(0x1E, [])])
                    except Exception:
                        pass
                    try:
                        frames.extend([dp._encode(dp.CMD_MANUAL_DOSE, 0x22, []),
                                       dp._encode(dp.CMD_MANUAL_DOSE, 0x1E, [])])
                    except Exception:
                        pass

                # Send probes (lightly spaced) then wait for a notify
                for idx, frame in enumerate(frames):
                    try:
                        await client.write_gatt_char(dp.UART_RX, frame, response=True)
                        _LOGGER.debug("sensor: sent totals probe %d/%d (len=%d)", idx + 1, len(frames), len(frame))
                    except Exception:
                        _LOGGER.debug("sensor: probe write failed (idx=%d)", idx, exc_info=True)
                    await asyncio.sleep(0.08)  # small gap; keep under SCAN_TIMEOUT budget

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
