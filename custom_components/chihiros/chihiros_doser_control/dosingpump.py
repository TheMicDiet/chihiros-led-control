from __future__ import annotations

from datetime import time
from typing import Tuple, List

from ..chihiros_led_control import commands
from .protocol import _split_ml_25_6  # use the same 25.6+0.1 encoder

def _clamp_byte(v: int) -> int:
    if not isinstance(v, int):
        raise TypeError(f"Parameter must be int (got {type(v).__name__})")
    if v < 0 or v > 255:
        raise ValueError(f"Parameter byte out of range 0..255: {v}")
    return v

def _bump_msg_id(msg_hi: int, msg_lo: int) -> tuple[int, int]:
    lo = (msg_lo + 1) & 0xFF
    hi = msg_hi
    if lo == 0x5A:                # never 0x5A in msg-id low
        lo = (lo + 1) & 0xFF
    if lo == 0:                   # wrapped → bump hi
        hi = (hi + 1) & 0xFF
        if hi == 0x5A:            # never 0x5A in msg-id high
            hi = (hi + 1) & 0xFF
    return hi, lo

def _create_command_encoding_dosing_pump(
    cmd_id: int,
    cmd_mode: int,
    msg_id: tuple[int, int],
    parameters: list[int],
) -> bytearray:
    """
    Wire format:
      [cmd_id, 0x01, len(params)+5, msg_hi, msg_lo, cmd_mode, *params, checksum]
    Checksum is XOR over bytes 1..end-1 (same as led-control `commands._calculate_checksum`).
    If checksum == 0x5A, we bump the msg-id and retry (do NOT mutate payload bytes).
    """
    _clamp_byte(cmd_id); _clamp_byte(cmd_mode)
    msg_hi, msg_lo = msg_id
    _clamp_byte(msg_hi); _clamp_byte(msg_lo)
    ps = [_clamp_byte(x) for x in parameters]

    # try a few msg-ids until checksum != 0x5A
    for _ in range(8):
        frame = bytearray([cmd_id, 1, len(ps) + 5, msg_hi, msg_lo, cmd_mode] + ps)
        checksum = commands._calculate_checksum(frame) & 0xFF
        if checksum != 0x5A:
            return frame + bytes([checksum])
        msg_hi, msg_lo = _bump_msg_id(msg_hi, msg_lo)

    # last resort: return the last attempt
    return frame + bytes([checksum])

# -------------------------
# WRITE command creators
# -------------------------

def create_add_dosing_pump_command_manuell_ml(
    msg_id: tuple[int, int],
    ch_id: int,
    ch_ml_one: int,  # actually: hi in 25.6 mL units (0..255)
    ch_ml_two: int,  # actually: lo in 0.1 mL units (0..255)
) -> bytearray:
    """
    Manual one-shot dose:
      mode=27, params = [channel, 0, 0, hi_25_6, lo_0_1]
    """
    _clamp_byte(ch_id)
    _clamp_byte(ch_ml_one)
    _clamp_byte(ch_ml_two)
    return _create_command_encoding_dosing_pump(165, 27, msg_id, [ch_id, 0, 0, ch_ml_one, ch_ml_two])

def create_add_dosing_pump_command_manuell_ml_amount(
    msg_id: tuple[int, int],
    ch_id: int,
    ml: float | int | str,
) -> bytearray:
    """
    Convenience: pass ml directly (0.2..999.9), we split to (hi, lo).
    """
    hi, lo = _split_ml_25_6(ml)
    return create_add_dosing_pump_command_manuell_ml(msg_id, ch_id, hi, lo)

def create_add_auto_setting_command_dosing_pump(
    performance_time: time,  # kept for API parity; real clock is set by the time command (mode 21)
    msg_id: tuple[int, int],
    ch_id: int,
    weekdays: int,
    ch_ml: int,  # 0.1 mL units from callers (e.g., 800 == 80.0 mL)
) -> bytearray:
    """
    Auto-setting entry with amount encoded as (hi, lo) per 25.6/0.1 scheme:
      mode=27, params = [channel, weekdays_mask, 1, 0, hi, lo]
    NOTE: `ch_ml` is tenths from the caller → convert to ml before splitting.
    """
    _clamp_byte(ch_id); _clamp_byte(weekdays)
    if ch_ml < 0:
        raise ValueError("ch_ml must be >= 0 (0.1 ml units)")
    ml = ch_ml / 10.0
    hi, lo = _split_ml_25_6(ml)
    return _create_command_encoding_dosing_pump(165, 27, msg_id, [ch_id, weekdays, 1, 0, hi, lo])

def create_auto_mode_dosing_pump_command_time(
    performance_time: time,
    msg_id: tuple[int, int],
    ch_id: int,
    timer_type: int = 1,  # 0 = single doses, 1 = 24-hour mode
) -> bytearray:
    """
    Set timer type and time:
      mode=21, params = [channel, timer_type, hour, minute, 0, 0]
    """
    _clamp_byte(ch_id); _clamp_byte(timer_type)
    return _create_command_encoding_dosing_pump(
        165, 21, msg_id, [ch_id, timer_type, performance_time.hour, performance_time.minute, 0, 0]
    )

def create_order_confirmation(
    msg_id: tuple[int, int],
    command_id: int,
    mode: int,
    command: int,
) -> bytearray:
    return _create_command_encoding_dosing_pump(command_id, mode, msg_id, [_clamp_byte(command)])

def create_reset_auto_settings_command(msg_id: tuple[int, int]) -> bytearray:
    """Reset auto settings (semantics depend on firmware)."""
    return _create_command_encoding_dosing_pump(90, 5, msg_id, [5, 255, 255])

def create_switch_to_auto_mode_dosing_pump_command(
   msg_id: tuple[int, int],
   channel_id: int,
   catch_up_missed: int = 0,   # 0/1 (Make up for missed dose)
   active_flag: int = 1,       # 0/1 (Inactive/Active)
) -> bytearray:
    """
    Switch dosing pump to auto mode for a specific channel:
      mode=32, params = [channel_id, catch_up_missed, active_flag]
    """
    _clamp_byte(channel_id); _clamp_byte(catch_up_missed); _clamp_byte(active_flag)
    return _create_command_encoding_dosing_pump(165, 32, msg_id, [channel_id, catch_up_missed, active_flag])
