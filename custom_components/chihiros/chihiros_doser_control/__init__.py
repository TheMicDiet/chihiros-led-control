from __future__ import annotations
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.exceptions import HomeAssistantError
from bleak import BleakClient

from ..const import DOMAIN  # existing const in your integration
from . import protocol as dp

DOSE_SCHEMA = vol.Schema({
    vol.Exclusive("device_id", "target"): str,
    vol.Exclusive("address", "target"): str,
    vol.Required("channel"): vol.All(int, vol.Range(min=1, max=4)),
    vol.Required("ml"): vol.All(float, vol.Range(min=0.2, max=999.9)),
})

async def _resolve_address_from_device_id(hass: HomeAssistant, did: str) -> str | None:
    reg = dr.async_get(hass)
    dev = reg.async_get(did)
    if not dev:
        return None
    # Prefer official BT connection tuple
    for (conn_type, conn_val) in dev.connections:
        if conn_type == dr.CONNECTION_BLUETOOTH:
            return conn_val
    # Or look through identifiers if you stash it under your DOMAIN
    for domain, ident in dev.identifiers:
        if domain == DOMAIN and ident.startswith("ble:"):
            return ident.split(":", 1)[1]
    return None

async def register_services(hass: HomeAssistant) -> None:
    # Use a flag to avoid duplicate registration on reloads
    flag_key = f"{DOMAIN}_doser_services_registered"
    if hass.data.get(flag_key):
        return
    hass.data[flag_key] = True

    async def _svc_dose(call: ServiceCall):
        # Validate a mutable copy (Voluptuous mutates; call.data is ReadOnlyDict)
        data = DOSE_SCHEMA(dict(call.data))
        addr = data.get("address")
        if not addr and (did := data.get("device_id")):
            addr = await _resolve_address_from_device_id(hass, did)
        if not addr:
            raise HomeAssistantError("Provide address or a device_id linked to a BLE address")

        async with BleakClient(addr, timeout=12.0) as client:
            await dp.dose_ml(client, int(data["channel"]), float(data["ml"]))

    hass.services.async_register(DOMAIN, "dose_ml", _svc_dose, schema=DOSE_SCHEMA)
