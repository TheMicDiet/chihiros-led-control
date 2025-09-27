from __future__ import annotations

import asyncio
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

# Accepts "2" or 2, "10.0" or 10.0 via Coerce; ranges match device limits.
DOSE_SCHEMA = vol.Schema({
    vol.Exclusive("device_id", "target"): str,
    vol.Exclusive("address", "target"): str,
    vol.Required("channel"): vol.All(vol.Coerce(int), vol.Range(min=1, max=4)),
    vol.Required("ml"):      vol.All(vol.Coerce(float), vol.Range(min=0.2, max=999.9)),
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


async def register_services(hass: HomeAssistant) -> None:
    # Avoid duplicate registration on reloads
    flag_key = f"{DOMAIN}_doser_services_registered"
    if hass.data.get(flag_key):
        return
    hass.data[flag_key] = True

    async def _svc_dose(call: ServiceCall):
        # Copy to allow voluptuous to coerce values
        data = DOSE_SCHEMA(dict(call.data))

        # Resolve address
        addr = data.get("address")
        if not addr and (did := data.get("device_id")):
            addr = await _resolve_address_from_device_id(hass, did)
        if not addr:
            raise HomeAssistantError("Provide address or a device_id linked to a BLE address")

        channel = int(data["channel"])
        ml = round(float(data["ml"]), 1)  # protocol is 0.1-mL resolution

        # IMPORTANT — protocol encoding reminder (documented for future edits):
        # The device encodes millilitres as two fields (hi, lo):
        #   hi = floor(ml / 25.6)                # 25.6-mL "bucket"
        #   lo = round((ml - hi*25.6) * 10)      # 0.1-mL remainder, 0..255
        # Examples: 11.3 → (0,113), 25.6 → (1,0), 51.2 → (2,0).
        # Do NOT split by 25.0 mL here. dp.dose_ml() handles the 25.6+0.1 scheme.

        # NEW: use HA’s bluetooth device lookup + bleak-retry-connector for reliable, slot-aware connections
        ble_dev = bluetooth.async_ble_device_from_address(hass, addr, True)
        if not ble_dev:
            raise HomeAssistantError(f"Could not find BLE device for address {addr}")

        # NEW: in the same BLE session as the dose, listen briefly for a totals frame
        # (5B/0x22 with 8 params). If we get one, decode and push it to sensors immediately.
        got: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

        def _on_notify(_char, payload: bytearray) -> None:
            try:
                if len(payload) < 8:
                    return
                cmd = payload[0]
                mode = payload[5] if len(payload) >= 6 else None
                params = list(payload[6:-1]) if len(payload) >= 8 else []
                if cmd in (0x5B, 91) and mode == 0x22 and len(params) == 8:
                    if not got.done():
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

            # OPTIONAL: some firmwares only reply on request — poke once.
            try:
                query = dp._encode(dp.CMD_MANUAL_DOSE, 0x22, [])  # params empty
                await client.write_gatt_char(dp.UART_RX, query, response=True)
            except Exception:
                # harmless if ignored
                pass

            # Wait briefly for a totals frame and broadcast to sensors if received
            try:
                payload = await asyncio.wait_for(got, timeout=2.5)
                params = list(payload[6:-1])
                pairs = list(zip(params[0::2], params[1::2]))
                mls = [round(h * 25.6 + l / 10.0, 1) for h, l in pairs]
                async_dispatcher_send(
                    hass,
                    f"{DOMAIN}_push_totals_{addr.lower()}",
                    {"ml": mls, "raw": payload},
                )
            except asyncio.TimeoutError:
                # No immediate totals — sensors can still poll later
                pass
            finally:
                try:
                    await client.stop_notify(UART_TX)
                except Exception:
                    pass

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
