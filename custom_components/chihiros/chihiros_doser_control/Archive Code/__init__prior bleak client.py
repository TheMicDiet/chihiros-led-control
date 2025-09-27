from __future__ import annotations

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.exceptions import HomeAssistantError
from bleak import BleakClient

from ..const import DOMAIN  # integration domain
from . import protocol as dp  # provides dose_ml(client, channel, ml)

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

        async with BleakClient(addr, timeout=12.0) as client:
            await dp.dose_ml(client, channel, ml)

    hass.services.async_register(DOMAIN, "dose_ml", _svc_dose, schema=DOSE_SCHEMA)
