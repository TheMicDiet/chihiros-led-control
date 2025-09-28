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

# NEW: human-friendly weekday handling using your existing encoding helper
from ..chihiros_led_control.weekday_encoding import (
    WeekdaySelect,
    encode_selected_weekdays,
)

_LOGGER = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────
# Schemas
# ────────────────────────────────────────────────────────────────

DOSE_SCHEMA = vol.Schema({
    vol.Exclusive("device_id", "target"): str,
    vol.Exclusive("address", "target"): str,
    vol.Required("channel"): vol.All(vol.Coerce(int), vol.Range(min=1, max=4)),
    vol.Required("ml"):      vol.All(vol.Coerce(float), vol.Range(min=0.2, max=999.9)),
})

READ_TOTALS_SCHEMA = vol.Schema({
    vol.Exclusive("device_id", "target"): str,
    vol.Exclusive("address", "target"): str,
})

# NEW: 24h config now accepts a real time-of-day and human-readable weekdays
SET_24H_SCHEMA = vol.Schema({
    vol.Exclusive("device_id", "target"): str,
    vol.Exclusive("address", "target"): str,

    vol.Required("channel"):   vol.All(vol.Coerce(int), vol.Range(min=1, max=4)),
    vol.Required("daily_ml"):  vol.All(vol.Coerce(float), vol.Range(min=0.2, max=999.9)),

    # Time-of-day (24h) — either provide "time": "HH:MM" OR hour+minute
    vol.Optional("time"): str,
    vol.Optional("hour", default=None): vol.Any(None, vol.All(vol.Coerce(int), vol.Range(min=0, max=23))),
    vol.Optional("minutes", default=None): vol.Any(None, vol.All(vol.Coerce(int), vol.Range(min=0, max=59))),

    # Days — either a numeric mask or human strings ("Mon,Wed", ["Mon","Thu"], "everyday")
    vol.Optional("weekday_mask", default=None): vol.Any(None, vol.All(vol.Coerce(int), vol.Range(min=0, max=0x7F))),
    vol.Optional("weekdays", default=None): vol.Any(str, [str]),

    vol.Optional("catch_up", default=False): vol.Boolean(),  # Make up for missed doses
})


# ────────────────────────────────────────────────────────────────
# Weekday helpers (English ⇄ mask) using your encoding
# ────────────────────────────────────────────────────────────────

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


# ────────────────────────────────────────────────────────────────
# Address / entry helpers
# ────────────────────────────────────────────────────────────────

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


# ────────────────────────────────────────────────────────────────
# Probe / prelude helpers
# ────────────────────────────────────────────────────────────────

def _build_totals_probes() -> list[bytes]:
    """Try 0x1E then 0x22, 0x5B-style first then A5-style as fallback."""
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

def _build_prelude_frames() -> list[bytes]:
    """Small ‘wake up / confirm’ sequence seen in captures."""
    return [
        dp._encode(90, 4, [1]),   # 90/4 [1]
        dp._encode(165, 4, [4]),  # 165/4 [4]
        dp._encode(165, 4, [5]),  # 165/4 [5]
    ]


# ────────────────────────────────────────────────────────────────
# Service registration
# ────────────────────────────────────────────────────────────────

async def register_services(hass: HomeAssistant) -> None:
    # Avoid duplicate registration on reloads
    flag_key = f"{DOMAIN}_doser_services_registered"
    if hass.data.get(flag_key):
        return
    hass.data[flag_key] = True

    # -------------------------
    # Manual one-shot dose
    # -------------------------
    async def _svc_dose(call: ServiceCall):
        data = DOSE_SCHEMA(dict(call.data))

        # Resolve address
        addr = data.get("address")
        if not addr and (did := data.get("device_id")):
            addr = await _resolve_address_from_device_id(hass, did)
        if not addr:
            raise HomeAssistantError("Provide address or a device_id linked to a BLE address")

        addr_u = addr.upper()
        addr_l = addr_u.lower()

        channel = int(data["channel"])
        ml = round(float(data["ml"]), 1)  # protocol is 0.1-mL resolution

        ble_dev = bluetooth.async_ble_device_from_address(hass, addr_u, True)
        if not ble_dev:
            raise HomeAssistantError(f"Could not find BLE device for address {addr_u}")

        got: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

        def _on_notify(_char, payload: bytearray) -> None:
            try:
                if isinstance(payload, (bytes, bytearray)) and len(payload) >= 6 and payload[0] in (0x5B, 91):
                    _LOGGER.debug("dose: notify 0x5B mode=0x%02X raw=%s",
                                  payload[5], bytes(payload).hex(" ").upper())
                vals = dp.parse_totals_frame(payload)
                if vals and not got.done():
                    got.set_result(bytes(payload))
            except Exception:
                pass

        client = None
        try:
            client = await establish_connection(BleakClientWithServiceCache, ble_dev, f"{DOMAIN}-dose")
            await client.start_notify(UART_TX, _on_notify)

            await dp.dose_ml(client, channel, ml)

            # Totals probes (0x1E & 0x22; 0x5B first; then A5)
            for frame in _build_totals_probes():
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
                vals = dp.parse_totals_frame(payload) or []
                async_dispatcher_send(hass, f"{DOMAIN}_push_totals_{addr_l}", {"ml": vals, "raw": payload})
            except asyncio.TimeoutError:
                _LOGGER.debug("dose: no totals frame received within timeout")

            try:
                await client.stop_notify(UART_TX)
            except Exception:
                pass

            if (entry_id := _find_entry_id_for_address(hass, addr_u)):
                async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_refresh_totals")

        except BLEAK_EXC as e:
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

    # -------------------------
    # Read & print daily totals
    # -------------------------
    async def _svc_read_totals(call: ServiceCall):
        data = READ_TOTALS_SCHEMA(dict(call.data))

        addr = data.get("address")
        if not addr and (did := data.get("device_id")):
            addr = await _resolve_address_from_device_id(hass, did)
        if not addr:
            raise HomeAssistantError("Provide address or a device_id linked to a BLE address")

        addr_u = addr.upper()
        addr_l = addr_u.lower()

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

            for frame in _build_totals_probes():
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
                msg = (
                    f"Daily totals for {addr_u}:\n"
                    f"  Ch1: {vals[0]} mL\n"
                    f"  Ch2: {vals[1]} mL\n"
                    f"  Ch3: {vals[2]} mL\n"
                    f"  Ch4: {vals[3]} mL"
                )
                await hass.services.async_call(
                    "persistent_notification", "create",
                    {"title": "Chihiros Doser — Daily totals", "message": msg},
                    blocking=False,
                )

                async_dispatcher_send(hass, f"{DOMAIN}_push_totals_{addr_l}", {"ml": vals, "raw": payload})

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

    # -------------------------
    # Configure 24-hour dosing
    # -------------------------
    async def _svc_set_24h(call: ServiceCall):
        data = SET_24H_SCHEMA(dict(call.data))

        # Resolve address
        addr = data.get("address")
        if not addr and (did := data.get("device_id")):
            addr = await _resolve_address_from_device_id(hass, did)
        if not addr:
            raise HomeAssistantError("Provide address or a device_id linked to a BLE address")

        addr_u = addr.upper()
        addr_l = addr_u.lower()

        channel = int(data["channel"])
        daily_ml = float(data["daily_ml"])
        catch_up = bool(data["catch_up"])

        # Parse time-of-day: prefer "time": "HH:MM", else hour+minutes
        hour: int | None = None
        minute: int | None = None
        if data.get("time"):
            try:
                parts = str(data["time"]).strip().split(":")
                hour = int(parts[0]); minute = int(parts[1])
            except Exception as e:
                raise HomeAssistantError(f"Invalid time format (expected HH:MM): {data['time']}") from e
        else:
            hour = data.get("hour")
            minute = data.get("minutes")

        if hour is None or minute is None:
            raise HomeAssistantError("Provide either 'time': 'HH:MM' or both 'hour' and 'minutes'.")

        if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
            raise HomeAssistantError("Time out of range: hour 0..23, minutes 0..59.")

        hour = int(hour)
        minute = int(minute)

        # Build weekday bitmask from human string/list or numeric mask
        weekday_mask = _parse_weekdays_to_mask(
            data.get("weekdays"),
            fallback_mask=(data.get("weekday_mask", 0x7F) if data.get("weekday_mask") is not None else 0x7F),
        )
        days_str = _weekdays_mask_to_english(weekday_mask)

        # Wire channel is 0-based in these frames
        wire_ch = max(1, min(channel, 4)) - 1

        # Split daily mL into (hi, lo) using the same 25.6 + 0.1 method
        hi = int(daily_ml // 25.6)
        lo = int(round((daily_ml - hi * 25.6) * 10))
        if lo == 256:
            hi += 1
            lo = 0
        hi &= 0xFF
        lo &= 0xFF

        # Frames (per your captures):
        #   0x15 → [ch, 1 /* 24h */, hour, minutes, 0, 0]
        #   0x1B → [ch, weekday_mask, 1 /*?*/, 0 /*completed today?*/, hi, lo]
        #   0x20 → [ch, 0 /*?*/, 1 if catch_up else 0]
        f_prelude  = _build_prelude_frames()
        f_schedule = dp._encode(dp.CMD_MANUAL_DOSE, 0x15, [wire_ch, 1, hour, minute, 0, 0])
        f_daily_ml = dp._encode(dp.CMD_MANUAL_DOSE, 0x1B, [wire_ch, weekday_mask, 1, 0, hi, lo])
        f_catchup  = dp._encode(dp.CMD_MANUAL_DOSE, 0x20, [wire_ch, 0, 1 if catch_up else 0])

        # Connect and apply
        ble_dev = bluetooth.async_ble_device_from_address(hass, addr_u, True)
        if not ble_dev:
            raise HomeAssistantError(f"Could not find BLE device for address {addr_u}")

        client = None
        try:
            client = await establish_connection(BleakClientWithServiceCache, ble_dev, f"{DOMAIN}-set-24h")

            # Send small prelude (stabilizes programming on some firmwares)
            for frame in f_prelude:
                try:
                    await client.write_gatt_char(dp.UART_RX, frame, response=True)
                except Exception:
                    pass
                await asyncio.sleep(0.08)

            # Then the actual 24h configuration
            for frame in (f_schedule, f_daily_ml, f_catchup):
                await client.write_gatt_char(dp.UART_RX, frame, response=True)
                await asyncio.sleep(0.10)

            # Notify user via persistent_notification (now shows time and English days)
            msg = (
                f"Configured 24-hour dosing for {addr_u}:\n"
                f"  Channel: {channel}\n"
                f"  Daily total: {daily_ml:.1f} mL\n"
                f"  Time: {hour:02d}:{minute:02d}\n"
                f"  Days: {days_str} (mask=0x{weekday_mask:02X})\n"
                f"  Catch-up: {'on' if catch_up else 'off'}"
            )
            await hass.services.async_call(
                "persistent_notification", "create",
                {"title": "Chihiros Doser — 24h program set", "message": msg},
                blocking=False,
            )

            # Ask sensors to refresh
            if (entry_id := _find_entry_id_for_address(hass, addr_u)):
                async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_refresh_totals")
            async_dispatcher_send(hass, f"{DOMAIN}_refresh_totals_{addr_l}")

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
