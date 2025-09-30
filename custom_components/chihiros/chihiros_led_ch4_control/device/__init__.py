from __future__ import annotations

# ────────────────────────────────────────────────────────────────
# Import guard so this module can be imported without Home Assistant
# (e.g. when running the CLI). We only define the real implementation
# if HA is available; otherwise we export a friendly stub.
# ────────────────────────────────────────────────────────────────
try:
    import asyncio
    import logging
   
    from homeassistant.core import HomeAssistant, ServiceCall
    from homeassistant.helpers import device_registry as dr
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    # use HA’s bluetooth helper + bleak-retry-connector (slot-aware, proxy-friendly)
    from homeassistant.components import bluetooth
    from bleak_retry_connector import (
        BleakClientWithServiceCache,
        BLEAK_RETRY_EXCEPTIONS as BLEAK_EXC,
        establish_connection,
    )

    from ..const import DOMAIN  # integration domain

    # human-friendly weekday handling using your existing encoding helper
    from ..chihiros_led_control.weekday_encoding import (
        WeekdaySelect,
        encode_selected_weekdays,
    )

    HA_AVAILABLE = True
except ModuleNotFoundError:
    HA_AVAILABLE = False


# ────────────────────────────────────────────────────────────────
# Public API when HA is *not* installed (CLI import-safe)
# ────────────────────────────────────────────────────────────────
if not HA_AVAILABLE:
    import logging

    _LOGGER = logging.getLogger(__name__)

    async def register_services(*_args, **_kwargs) -> None:
        raise RuntimeError(
            "Chihiros Ch4 services require Home Assistant. "
            "This module can be imported safely by the CLI, "
            "but service registration only works inside Home Assistant."
        )

# ────────────────────────────────────────────────────────────────
# Full implementation when HA *is* available
# ────────────────────────────────────────────────────────────────
else:
    _LOGGER = logging.getLogger(__name__)

    # ────────────────────────────────────────────────────────────
    # Schemas
    # ────────────────────────────────────────────────────────────
    CH4_SCHEMA = vol.Schema({
        vol.Exclusive("device_id", "target"): str,
        vol.Exclusive("address", "target"): str,
        vol.Required("channel"): vol.All(vol.Coerce(int), vol.Range(min=1, max=4)),
    })


    # ────────────────────────────────────────────────────────────
    # Weekday helpers (English ⇄ mask) using your encoding
    # ────────────────────────────────────────────────────────────
    _WEEKDAY_ALIAS = {
        "mon": WeekdaySelect.monday, "monday": WeekdaySelect.monday,
        "tue": WeekdaySelect.tuesday, "tues": WeekdaySelect.tuesday, "tuesday": WeekdaySelect.tuesday,
        "wed": WeekdaySelect.wednesday, "wednesday": WeekdaySelect.wednesday,
        "thu": WeekdaySelect.thursday, "thur": WeekdaySelect.thursday, "thurs": WeekdaySelect.thursday, "thursday": WeekdaySelect.thursday,
        "fri": WeekdaySelect.friday, "friday": WeekdaySelect.friday,
        "sat": WeekdaySelect.saturday, "saturday": WeekdaySelect.saturday,
        "sun": WeekdaySelect.sunday, "sunday": WeekdaySelect.sunday,
        "everyday": "ALL", "every day": "ALL", "any": "ALL", "all": "ALL",
    }

    def _parse_weekdays_to_mask(value, fallback_mask: int = 0x7F) -> int:
        """Accept int mask, string 'Mon,Wed,Fri', or list of day strings; return 0..127 mask."""
        if value is None:
            return fallback_mask & 0x7F
        if isinstance(value, int):
            return value & 0x7F
        if isinstance(value, str):
            tokens = [t.strip() for t in value.replace("/", ",").split(",") if t.strip()]
        else:
            try:
                tokens = [str(t).strip() for t in value]
            except Exception:
                return fallback_mask & 0x7F

        if any(_WEEKDAY_ALIAS.get(t.lower()) == "ALL" for t in tokens):
            return 127

        sels: list[WeekdaySelect] = []
        for t in tokens:
            sel = _WEEKDAY_ALIAS.get(t.lower())
            if isinstance(sel, WeekdaySelect):
                sels.append(sel)
        return encode_selected_weekdays(sels) if sels else (fallback_mask & 0x7F)

    def _weekdays_mask_to_english(mask: int) -> str:
        """Render mask (Mon=64 … Sun=1) as 'Mon, Tue, …' or 'Every day'."""
        try:
            m = int(mask) & 0x7F
        except Exception:
            return "Unknown"
        parts = []
        if m & 64: parts.append("Mon")
        if m & 32: parts.append("Tue")
        if m & 16: parts.append("Wed")
        if m & 8:  parts.append("Thu")
        if m & 4:  parts.append("Fri")
        if m & 2:  parts.append("Sat")
        if m & 1:  parts.append("Sun")
        return "Every day" if len(parts) == 7 else (", ".join(parts) if parts else "None")

    # ────────────────────────────────────────────────────────────
    # Address / entry helpers
    # ────────────────────────────────────────────────────────────
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

    def _find_entry_id_for_address(hass: HomeAssistant, addr: str) -> str | None:
        """Find the config_entry_id for a BLE address (case-insensitive)."""
        data_by_entry = hass.data.get(DOMAIN, {})
        addr_l = (addr or "").lower()
        for entry_id, data in data_by_entry.items():
            coord = getattr(data, "coordinator", None)
            c_addr = getattr(coord, "address", None)
            if isinstance(c_addr, str) and c_addr.lower() == addr_l:
                return entry_id
        return None



    # ────────────────────────────────────────────────────────────
    # Service registration
    # ────────────────────────────────────────────────────────────
    async def register_services(hass: HomeAssistant) -> None:
        # Avoid duplicate registration on reloads
        flag_key = f"{DOMAIN}_ch4_services_registered"
        if hass.data.get(flag_key):
            return
        hass.data[flag_key] = True

    