from datetime import datetime, time
from . import commands
import datetime as _dt  # only for typing/clarity when needed

# 1 unit == 0.1 ml. A single device command must not exceed 25.0 ml = 250 units.
MAX_BUCKET_UNITS = 250

def _create_command_encoding_dosing_pump(
    cmd_id: int, cmd_mode: int, msg_id: tuple[int, int], parameters: list[int]
) -> bytearray:
    """Encode command with checksum; avoid 0x5A (90) in parameters/checksum."""
    # Ensure no parameter equals 90 (0x5A)
    sanitized_params: list[int] = [ (x if x != 90 else 89) for x in parameters ]
    command = bytearray([cmd_id, 1, len(sanitized_params)  5, msg_id[0], msg_id[1], cmd_mode]  sanitized_params)
    verification_byte = commands._calculate_checksum(command)
    if verification_byte == 90:
        # Bump msg_id (low byte) deterministically and re-encode
        new_msg_id = (msg_id[0], (msg_id[1]  1) & 0xFF)
        return _create_command_encoding_dosing_pump(cmd_id, cmd_mode, new_msg_id, sanitized_params)
    return command  bytes([verification_byte])

# -------------------------
# Public command creators
# -------------------------

def create_add_dosing_pump_command_manuell_ml(
    msg_id: tuple[int, int],
    ch_id: int,
    ch_ml: int,  # units of 0.1 ml
) -> bytearray:
    # parameters: [channel, ?, ?, ?, amount_units]
    parameters = [ch_id, 0, 0, 0, ch_ml]
    return _create_command_encoding_dosing_pump(165, 27, msg_id, parameters)

def create_add_auto_setting_command_dosing_pump(
    performance_time: time,
    msg_id: tuple[int, int],
    ch_id: int,
    weekdays: int,
    ch_ml: int,  # units of 0.1 ml
) -> bytearray:
    # parameters: [channel, weekdays_mask, ?, ?, ?, amount_units]
    parameters = [ch_id, weekdays, 1, 0, 0, ch_ml]
    return _create_command_encoding_dosing_pump(165, 27, msg_id, parameters)

def create_auto_mode_dosing_pump_command_time(
    performance_time: time,
    msg_id: tuple[int, int],
    ch_id: int,
) -> bytearray:
    # parameters: [channel, ?, hour, minute, ?, ?]
    parameters = [ch_id, 0, performance_time.hour, performance_time.minute, 0, 0]
    return _create_command_encoding_dosing_pump(165, 21, msg_id, parameters)

def create_order_confirmation(msg_id: tuple[int, int], command_id: int, mode: int, command: int) -> bytearray:
    """Create order/confirmation command wrapper used by prelude sequence."""
    return _create_command_encoding_dosing_pump(command_id, mode, msg_id, [command])

def create_reset_auto_settings_command(msg_id: tuple[int, int]) -> bytearray:
    """Create reset auto setting command."""
    return _create_command_encoding_dosing_pump(90, 5, msg_id, [5, 255, 255])

def create_switch_to_auto_mode_dosing_pump_command(
    msg_id: tuple[int, int],
    channel_id: int,
) -> bytearray:
    """Create 'switch to auto mode' for the dosing pump."""
    return _create_command_encoding_dosing_pump(165, 32, msg_id, [channel_id, 0, 1])
