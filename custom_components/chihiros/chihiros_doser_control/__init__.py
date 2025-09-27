from __future__ import annotations

import asyncio
import logging
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.exceptions import HomeAssistantError

# NEW: push updated totals straight to the sensors after a dose
from homeassistant.helpers.dispatcher import async_dispatcher_send

# NEW: use HA’s bluetooth helper + bleak-retry-connector (slot-aware, proxy-friendly)
from homeassistant.components import bluetooth
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BLEAK_RETRY_EXCEPTIONS as BLEAK_EXC,
    establish_connection,
)

from ..const import DOMAIN  # integration domain
from . import protocol as dp  # provides dose_ml(client, channel, ml)
from .protocol import UART_TX  # NEW: notify UUID for totals frames

_LOGGER = logging.getLogger(__name__)

# Accepts "2" or 2, "10.0" or 10.0 via Coerce; ranges match device limits.
DOSE_SCHEMA = vol.Schema({
    vol.Exclusive("device_id", "target"): str,
    vol.Exclusive("address", "target"): str,
    vol.Required("channel"): vol.All(vol.Coerce(int), vol.Range(min=1, max=4)),
    vol.Required("ml"):      vol.All(vol.Coerce(float), vol.Range(min=0.2, max=999.9)),
})

# NEW: schema for read_daily_totals (either device_id or address)
READ_TOTALS_SCHEMA = vol.Schema({
    vol.Exclusive("device_id", "target"): str,
    vol.Exclusive("address", "target"): str,
})

# NEW: schema to configure 24h automatic dosing
SET_24H_SCHEMA = vol.Schema({
    vol.Exclusive("device_id", "target"): str,
    vol.Exclusive("address", "target"): str,
    vol.Required("channel"):   vol.All(vol.Coerce(int), vol.Range(min=1, max=4)),
    vol.Required("daily_ml"):  vol.All(vol.Coerce(float), vol.Range(min=0.2, max=999.9)),
    vol.Required("minutes"):   vol.All(vol.Coerce(int), vol.Range(min=1, max=59)),  # Minutes only: 00..59
    vol.Optional("catch_up", default=False): vol.Boolean(),  # Make up for missed doses
    # For now we default to Any day (0x7F). If you want per-day later, we can add a list→bitmask mapper.
    vol.Optional("weekday_mask", default=0x7F): vol.All(vol.Coerce(int), vol.Range(min=0, max=0x7F)),
})


async def _resolve_address_from_device_id(hass: HomeAssistant, did: str) -> str | None:
    reg = dr.async_get(hass)
    dev = reg.async_get(did)
    if not dev:
        return None

    # 1) Native BT connection stored by HA
    for (conn_type, conn_val) in dev.connections:
        if conn_type == dr.CONNECTION_BLUETOOTH:
            return conn_val

    # 2) Identifiers used by this integration
    for domain, ident in dev.identifiers:
        if domain != DOMAIN:
            continue
        # a) explicit "ble:AA:BB:..."
        if isinstance(ident, str) and ident.startswith("ble:"):
            return ident.split(":", 1)[1]
        # b) (DOMAIN, <config_entry_id>) → hass.data[DOMAIN][entry_id].coordinator.address
        data_by_entry = hass.data.get(DOMAIN, {})
        data = data_by_entry.get(ident)
        if data and hasattr(data.coordinator, "address"):
            return data.coordinator.address

    # 3) Fallback: any linked config entries
    for entry_id in getattr(dev, "config_entries", set()):
        data_by_entry = hass.data.get(DOMAIN, {})
        data = data_by_entry.get(entry_id)
        if data and hasattr(data.coordinator, "address"):
            return data.coordinator.address

    return None

# NEW: find the config_entry_id for a given BLE address so we can emit the
# entry-id–scoped refresh signal that sensor.py listens for.
def _find_entry_id_for_address(hass: HomeAssistant, addr: str) -> str | None:
    data_by_entry = hass.data.get(DOMAIN, {})
    addr_l = (addr or "").lower()
    for entry_id, data in data_by_entry.items():
        coord = getattr(data, "coordinator", None)
        c_addr = getattr(coord, "address", None)
        if isinstance(c_addr, str) and c_addr.lower() == addr_l:
            return entry_id
    return None

# NEW: helper to build probe frames (try 0x1E then 0x22, 0x5B first then A5)
def _build_totals_probes() -> list[bytes]:
    frames: list[bytes] = []
    try:
        if hasattr(dp, "encode_5b"):
            frames.append(dp.encode_5b(0x1E, []))  # some firmwares use 0x1E
            frames.append(dp.encode_5b(0x22, []))  # others use 0x22
    except Exception:
        pass
    try:
        frames.append(dp._encode(dp.CMD_MANUAL_DOSE, 0x1E, []))
        frames.append(dp._encode(dp.CMD_MANUAL_DOSE, 0x22, []))
    except Exception:
        pass
    return frames


async def register_services(hass: HomeAssistant) -> None:
    # Avoid duplicate registration on reloads
    flag_key = f"{DOMAIN}_doser_services_registered"
    if hass.data.get(flag_key):
        return
    hass.data[flag_key] = True

    # ────────────────────────────────────────────────────────────────
    # Existing: one-shot manual dose
    # ────────────────────────────────────────────────────────────────
    async def _svc_dose(call: ServiceCall):
        # Copy to allow voluptuous to coerce values
        data = DOSE_SCHEMA(dict(call.data))

        # Resolve address
        addr = data.get("address")
        if not addr and (did := data.get("device_id")):
            addr = await _resolve_address_from_device_id(hass, did)
        if not addr:
            raise HomeAssistantError("Provide address or a device_id linked to a BLE address")

        # IMPORTANT — normalize address to uppercase for HA’s bluetooth registry
        addr_u = addr.upper()

        channel = int(data["channel"])
        ml = round(float(data["ml"]), 1)  # protocol is 0.1-mL resolution

        # IMPORTANT — protocol encoding reminder (documented for future edits):
        # The device encodes millilitres as two fields (hi, lo):
        #   hi = floor(ml / 25.6)                # 25.6-mL "bucket"
        #   lo = round((ml - hi*25.6) * 10)      # 0.1-mL remainder, 0..255
        # Examples: 11.3 → (0,113), 25.6 → (1,0), 51.2 → (2,0).
        # Do NOT split by 25.0 mL here. dp.dose_ml() handles the 25.6+0.1 scheme.

        # NEW: use HA’s bluetooth device lookup + bleak-retry-connector for reliable, slot-aware connections
        ble_dev = bluetooth.async_ble_device_from_address(hass, addr_u, True)
        if not ble_dev:
            raise HomeAssistantError(f"Could not find BLE device for address {addr_u}")

        # NEW: in the same BLE session as the dose, listen briefly for a totals frame
        # (5B/0x22 or 5B/0x1E with 8+ params). If we get one, decode and push it to sensors immediately.
        got: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

        def _on_notify(_char, payload: bytearray) -> None:
            try:
                # Log any 0x5B frames for debugging
                if isinstance(payload, (bytes, bytearray)) and len(payload) >= 6 and payload[0] in (0x5B, 91):
                    _LOGGER.debug("dose: notify 0x5B mode=0x%02X raw=%s",
                                  payload[5], bytes(payload).hex(" ").upper())
                vals = dp.parse_totals_frame(payload)
                if vals and not got.done():
                    got.set_result(bytes(payload))
            except Exception:
                # never raise from callback
                pass

        client = None
        try:
            # The name helps HA’s bluetooth stack track/reuse a slot
            client = await establish_connection(BleakClientWithServiceCache, ble_dev, f"{DOMAIN}-dose")

            # Start notify *before* dosing so we don’t miss a quick totals push
            await client.start_notify(UART_TX, _on_notify)

            # Writes via protocol helper (uses client.write_gatt_char on UART_RX)
            await dp.dose_ml(client, channel, ml)

            # OPTIONAL: some firmwares only reply on request — send totals probes (0x1E & 0x22)
            probes = _build_totals_probes()
            for frame in probes:
                try:
                    # Prefer RX; if that errors, try TX as a fallback (some stacks mislabel)
                    try:
                        await client.write_gatt_char(dp.UART_RX, frame, response=True)
                    except Exception:
                        await client.write_gatt_char(dp.UART_TX, frame, response=True)
                except Exception:
                    pass
                await asyncio.sleep(0.08)

            # Wait briefly for a totals frame and broadcast to sensors if received
            try:
                payload = await asyncio.wait_for(got, timeout=8.0)
                vals = dp.parse_totals_frame(payload) or []
                async_dispatcher_send(
                    hass,
                    f"{DOMAIN}_push_totals_{addr.lower()}",
                    {"ml": vals, "raw": payload},
                )
            except asyncio.TimeoutError:
                # No immediate totals — sensors can still poll later
                _LOGGER.debug("dose: no totals frame received within timeout")

            # Stop notify
            try:
                await client.stop_notify(UART_TX)
            except Exception:
                pass

            # NEW: regardless of push success, also nudge sensors to refresh via BLE.
            # Sensor listens on entry-id–scoped signal: f"{DOMAIN}_{entry_id}_refresh_totals"
            if (entry_id := _find_entry_id_for_address(hass, addr_u)):
                async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_refresh_totals")

        except BLEAK_EXC as e:
            # Connection slot / transient BLE issues get normalized into a user error
            raise HomeAssistantError(f"BLE temporarily unavailable: {e}") from e
        except Exception as e:
            raise HomeAssistantError(f"Dose failed: {e}") from e
        finally:
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass

    hass.services.async_register(DOMAIN, "dose_ml", _svc_dose, schema=DOSE_SCHEMA)

    # ────────────────────────────────────────────────────────────────
    # NEW: read & print daily totals service
    # ────────────────────────────────────────────────────────────────
    async def _svc_read_totals(call: ServiceCall):
        data = READ_TOTALS_SCHEMA(dict(call.data))

        addr = data.get("address")
        if not addr and (did := data.get("device_id")):
            addr = await _resolve_address_from_device_id(hass, did)
        if not addr:
            raise HomeAssistantError("Provide address or a device_id linked to a BLE address")

        addr_u = addr.upper()

        # Use HA bluetooth + bleak-retry-connector for a slot-aware connection
        ble_dev = bluetooth.async_ble_device_from_address(hass, addr_u, True)
        if not ble_dev:
            raise HomeAssistantError(f"Could not find BLE device for address {addr_u}")

        got: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

        def _on_notify(_char, payload: bytearray) -> None:
            try:
                if isinstance(payload, (bytes, bytearray)) and len(payload) >= 6 and payload[0] in (0x5B, 91):
                    _LOGGER.debug("read: notify 0x5B mode=0x%02X raw=%s",
                                  payload[5], bytes(payload).hex(" ").upper())
                vals = dp.parse_totals_frame(payload)
                if vals and not got.done():
                    got.set_result(bytes(payload))
            except Exception:
                pass

        client = None
        try:
            client = await establish_connection(BleakClientWithServiceCache, ble_dev, f"{DOMAIN}-read-totals")
            await client.start_notify(UART_TX, _on_notify)

            # Send totals probes (0x1E & 0x22; 0x5B first then A5)
            probes = _build_totals_probes()
            for frame in probes:
                try:
                    try:
                        await client.write_gatt_char(dp.UART_RX, frame, response=True)
                    except Exception:
                        await client.write_gatt_char(dp.UART_TX, frame, response=True)
                except Exception:
                    pass
                await asyncio.sleep(0.08)

            try:
                payload = await asyncio.wait_for(got, timeout=8.0)
                vals = dp.parse_totals_frame(payload) or [None, None, None, None]
                # Print via persistent_notification (visible in UI)
                msg = (
                    f"Daily totals for {addr_u}:\n"
                    f"  Ch1: {vals[0]} mL\n"
                    f"  Ch2: {vals[1]} mL\n"
                    f"  Ch3: {vals[2]} mL\n"
                    f"  Ch4: {vals[3]} mL"
                )
                await hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {"title": "Chihiros Doser — Daily totals", "message": msg},
                    blocking=False,
                )

                # Push to sensors immediately
                async_dispatcher_send(
                    hass,
                    f"{DOMAIN}_push_totals_{addr.lower()}",
                    {"ml": vals, "raw": payload},
                )

                # Also nudge the entry-id refresh path (if any)
                if (entry_id := _find_entry_id_for_address(hass, addr_u)):
                    async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_refresh_totals")

            except asyncio.TimeoutError:
                raise HomeAssistantError("No totals frame received (timeout). Try again.")
            finally:
                try:
                    await client.stop_notify(UART_TX)
                except Exception:
                    pass

        except BLEAK_EXC as e:
            raise HomeAssistantError(f"BLE temporarily unavailable: {e}") from e
        except Exception as e:
            raise HomeAssistantError(f"Totals read failed: {e}") from e
        finally:
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass

    hass.services.async_register(DOMAIN, "read_daily_totals", _svc_read_totals, schema=READ_TOTALS_SCHEMA)

    # ────────────────────────────────────────────────────────────────
    # NEW: configure 24-hour automatic dosing program
    # ────────────────────────────────────────────────────────────────
    async def _svc_set_24h(call: ServiceCall):
        data = SET_24H_SCHEMA(dict(call.data))

        # Resolve address
        addr = data.get("address")
        if not addr and (did := data.get("device_id")):
            addr = await _resolve_address_from_device_id(hass, did)
        if not addr:
            raise HomeAssistantError("Provide address or a device_id linked to a BLE address")

        addr_u = addr.upper()
        channel = int(data["channel"])
        minutes = int(data["minutes"])              # 1..59
        daily_ml = float(data["daily_ml"])
        catch_up = bool(data["catch_up"])
        weekday_mask = int(data["weekday_mask"])    # default 0x7F (“Any day”)

        # Bounds already validated by schema, but clamp defensively
        minutes = max(1, min(minutes, 59))

        # Wire channel is 0-based on this protocol (manual dose uses 0-based too)
        wire_ch = max(1, min(channel, 4)) - 1

        # Split daily mL into (hi, lo) using the same 25.6 + 0.1 method
        # hi = floor(ml/25.6); lo = round((ml - hi*25.6)*10)
        hi = int(daily_ml // 25.6)
        lo = int(round((daily_ml - hi * 25.6) * 10))
        if lo == 256:  # normalize exact multiple
            hi += 1
            lo = 0
        hi &= 0xFF
        lo &= 0xFF

        # Frames (decimal samples you posted map to these modes):
        #   0x15 → [ch, 1 /* 24h */, 0 /*hour*/, minutes, 0, 0]
        #   0x1B → [ch, weekday_mask, 1 /*?*/, 0 /*completed today?*/, hi, lo]
        #   0x20 → [ch, 0 /*?*/, 1 if catch_up else 0]
        f_schedule  = dp._encode(dp.CMD_MANUAL_DOSE, 0x15, [wire_ch, 1, 0, minutes, 0, 0])
        f_daily_ml  = dp._encode(dp.CMD_MANUAL_DOSE, 0x1B, [wire_ch, weekday_mask, 1, 0, hi, lo])
        f_catchup   = dp._encode(dp.CMD_MANUAL_DOSE, 0x20, [wire_ch, 0, 1 if catch_up else 0])

        # Connect and apply
        ble_dev = bluetooth.async_ble_device_from_address(hass, addr_u, True)
        if not ble_dev:
            raise HomeAssistantError(f"Could not find BLE device for address {addr_u}")

        client = None
        try:
            client = await establish_connection(BleakClientWithServiceCache, ble_dev, f"{DOMAIN}-set-24h")
            # Send in a stable order, small pacing between writes
            for frame in (f_schedule, f_daily_ml, f_catchup):
                await client.write_gatt_char(dp.UART_RX, frame, response=True)
                await asyncio.sleep(0.1)

            # Notify user via persistent_notification
            msg = (
                f"Configured 24-hour dosing for {addr_u}:\n"
                f"  Channel: {channel}\n"
                f"  Daily total: {daily_ml:.1f} mL\n"
                f"  Interval: every {minutes} min\n"
                f"  Days: 0x{weekday_mask:02X} (127=Any)\n"
                f"  Catch-up: {'on' if catch_up else 'off'}"
            )
            await hass.services.async_call(
                "persistent_notification", "create",
                {"title": "Chihiros Doser — 24h program set", "message": msg},
                blocking=False,
            )

            # Ask sensors to refresh (pull a totals snapshot if available)
            if (entry_id := _find_entry_id_for_address(hass, addr_u)):
                async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_refresh_totals")

        except BLEAK_EXC as e:
            raise HomeAssistantError(f"BLE temporarily unavailable: {e}") from e
        except Exception as e:
            raise HomeAssistantError(f"Failed to set 24-hour dosing: {e}") from e
        finally:
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass

    hass.services.async_register(DOMAIN, "set_24h_dose", _svc_set_24h, schema=SET_24H_SCHEMA)
