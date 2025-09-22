from __future__ import annotations
import asyncio
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
    vol.Required("channel"): vol.All(vol.Coerce(int), vol.Range(min=1, max=4)),
    vol.Required("ml"):      vol.All(vol.Coerce(float), vol.Range(min=0.2, max=999.9)),
})

async def _resolve_address_from_device_id(hass: HomeAssistant, did: str) -> str | None:
    reg = dr.async_get(hass)
    dev = reg.async_get(did)
    if not dev:
        return None

    # 1) If HA stored a native BT connection, use it.
    for (conn_type, conn_val) in dev.connections:
        if conn_type == dr.CONNECTION_BLUETOOTH:
            return conn_val

    # 2) Check our identifiers
    for domain, ident in dev.identifiers:
        if domain != DOMAIN:
            continue
        # a) explicit "ble:AA:BB:..." identifier
        if ident.startswith("ble:"):
            return ident.split(":", 1)[1]
        # b) we use (DOMAIN, <config_entry_id>) — look it up in hass.data
        data_by_entry = hass.data.get(DOMAIN, {})
        data = data_by_entry.get(ident)
        if data and hasattr(data.coordinator, "address"):
            return data.coordinator.address

    # 3) Last resort: any config entries linked to this device
    for entry_id in getattr(dev, "config_entries", set()):
        data_by_entry = hass.data.get(DOMAIN, {})
        data = data_by_entry.get(entry_id)
        if data and hasattr(data.coordinator, "address"):
            return data.coordinator.address

    return None

async def register_services(hass: HomeAssistant) -> None:
    # Use a flag to avoid duplicate registration on reloads
    flag_key = f"{DOMAIN}_doser_services_registered"
    if hass.data.get(flag_key):
        return
    hass.data[flag_key] = True

    async def _svc_dose(call: ServiceCall):
        # validate & copy (ServiceCall.data is ReadOnlyDict; voluptuous may mutate)
        data = DOSE_SCHEMA(dict(call.data))
        # Prefer explicit address, else resolve from device_id
        addr = data.get("address")
        if not addr and (did := data.get("device_id")):
            addr = await _resolve_address_from_device_id(hass, did)
        if not addr:
            raise HomeAssistantError("Provide address or a device_id linked to a BLE address")

        channel = int(data["channel"])
        remaining = float(data["ml"])
        # Send doses in ≤25.0 mL chunks to match device frame limits
        async with BleakClient(addr, timeout=12.0) as client:
            while remaining > 0:
                step = 25.0 if remaining > 25.0 else remaining
                await dp.dose_ml(client, channel, step)
                remaining -= step
                if remaining > 0:
                    # brief pause helps some BLE stacks/proxies
                    await asyncio.sleep(0.25)

    hass.services.async_register(DOMAIN, "dose_ml", _svc_dose, schema=DOSE_SCHEMA)
