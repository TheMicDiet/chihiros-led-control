"""Module defining commands generation functions."""

import datetime


def next_message_id(current_msg_id: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    """Generate bluetooth message id."""
    msg_id_higher_byte, msg_id_lower_byte = current_msg_id
    if msg_id_lower_byte == 255:
        if msg_id_higher_byte == 255:
            # start counting from the beginning
            return (0, 1)
        if msg_id_higher_byte == 89:
            # higher byte should never be 90
            return (msg_id_higher_byte + 2, msg_id_lower_byte)
        return (msg_id_higher_byte + 1, 0)

    else:
        if msg_id_lower_byte == 89:
            # lower byte should never be 90
            return (0, msg_id_lower_byte + 2)
        return (0, msg_id_lower_byte + 1)


def _calculate_checksum(input_bytes: bytes) -> int:
    """Calculate message checksum."""
    assert len(input_bytes) >= 7  # commands are always at least 7 bytes long
    checksum = input_bytes[1]
    for input_byte in input_bytes[2:]:
        checksum = checksum ^ input_byte
    return checksum


def _create_command_encoding(
    cmd_id: int, cmd_mode: int, msg_id: tuple[int, int], parameters: list[int]
) -> bytearray:
    """Encode command."""
    # make sure that no parameter is 90
    sanitized_params: list[int] = list(map(lambda x: x if x != 90 else 89, parameters))

    command = bytearray(
        [cmd_id, 1, len(parameters) + 5, msg_id[0], msg_id[1], cmd_mode]
        + sanitized_params
    )

    verification_byte = _calculate_checksum(command)
    if verification_byte == 90:
        # make sure that verification byte is not 90
        new_msg_id = (msg_id[0], msg_id[1] + 1)
        return _create_command_encoding(cmd_id, cmd_mode, new_msg_id, sanitized_params)

    return command + bytes([verification_byte])


def _encode_timestamp(ts: datetime.datetime) -> list[int]:
    """Encode timestamp."""
    # note: day is weekday e.g. 3 for wednesday
    return [ts.year - 2000, ts.month, ts.isoweekday(), ts.hour, ts.minute, ts.second]


def create_set_time_command(msg_id: tuple[int, int]) -> bytearray:
    """Create current time command."""
    return _create_command_encoding(
        90, 9, msg_id, _encode_timestamp(datetime.datetime.now())
    )


def create_manual_setting_command(
    msg_id: tuple[int, int], color: int, brightness_level: int
) -> bytearray:
    """Set brightness.

    param: color: 0-2 (0 is red, 1 is green, 2 is blue; on non-RGB models, 0 is white)
    param: brightness_level: 0 - 100
    """
    return _create_command_encoding(90, 7, msg_id, [color, brightness_level])


def create_add_auto_setting_command(
    msg_id: tuple[int, int],
    sunrise: datetime.time,
    sunset: datetime.time,
    brightness: tuple[int, int, int],
    ramp_up_minutes: int,
    weekdays: int,
) -> bytearray:
    """Add auto setting.

    brightness: tuple of 3 ints for red, green, and blue brightness, respectively
                on non-RGB models, set to (white brightness, 255, 255)
    weekdays: int resulting of selection bit mask
              (Monday Tuesday Wednesday Thursday Friday Saturday Sunday) in decimal
    """
    parameters = [
        sunrise.hour,
        sunrise.minute,
        sunset.hour,
        sunset.minute,
        ramp_up_minutes,
        weekdays,
        *brightness,
        255,
        255,
        255,
        255,
        255,
    ]

    return _create_command_encoding(165, 25, msg_id, parameters)


def create_delete_auto_setting_command(
    msg_id: tuple[int, int],
    sunrise: datetime.time,
    sunset: datetime.time,
    ramp_up_minutes: int,
    weekdays: int,
) -> bytearray:
    """Create delete auto setting command."""
    return create_add_auto_setting_command(
        msg_id, sunrise, sunset, (255, 255, 255), ramp_up_minutes, weekdays
    )


def create_reset_auto_settings_command(msg_id: tuple[int, int]) -> bytearray:
    """Create reset auto setting command."""
    return _create_command_encoding(90, 5, msg_id, [5, 255, 255])


def create_switch_to_auto_mode_command(msg_id: tuple[int, int]) -> bytearray:
    """Create switch auto setting command."""
    return _create_command_encoding(90, 5, msg_id, [18, 255, 255])

def _clamp_byte(v: int) -> int:
    if not isinstance(v, int):
        raise TypeError(f"Parameter must be int (got {type(v).__name__})")
    if v < 0 or v > 255:
        raise ValueError(f"Parameter byte out of range 0..255: {v}")
    return v

def create_order_confirmation(
    msg_id: tuple[int, int],
    command_id: int,
    mode: int,
    command: int,
) -> bytearray:
    return _create_command_encoding(command_id, mode, msg_id, [_clamp_byte(command)])

def _create_command_encoding(
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
        checksum = _calculate_checksum(frame) & 0xFF
        if checksum != 0x5A:
            return frame + bytes([checksum])
        msg_hi, msg_lo = _bump_msg_id(msg_hi, msg_lo)

    # last resort: return the last attempt
    return frame + bytes([checksum])

def create_switch_to_manuell_mode_command(msg_id: tuple[int, int]) -> bytearray:
    """Create switch auto setting command."""
    return _create_command_encoding(90, 5, msg_id, [11, 255, 255])
