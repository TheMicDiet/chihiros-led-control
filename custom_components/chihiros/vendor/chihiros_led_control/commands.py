"""High-level Chihiros command builders."""

from __future__ import annotations

import datetime

from .protocol import create_command_encoding, encode_timestamp


def create_set_time_command(msg_id: tuple[int, int]) -> bytearray:
    """Create the current time command."""
    return create_command_encoding(90, 9, msg_id, encode_timestamp(datetime.datetime.now()))


def create_manual_setting_command(msg_id: tuple[int, int], color: int, brightness_level: int) -> bytearray:
    """Create a manual brightness command."""
    return create_command_encoding(90, 7, msg_id, [color, brightness_level])


def create_add_auto_setting_command(
    msg_id: tuple[int, int],
    sunrise: datetime.time,
    sunset: datetime.time,
    brightness: tuple[int, int, int],
    ramp_up_minutes: int,
    weekdays: int,
) -> bytearray:
    """Create an add auto setting command."""
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

    return create_command_encoding(165, 25, msg_id, parameters)


def create_delete_auto_setting_command(
    msg_id: tuple[int, int],
    sunrise: datetime.time,
    sunset: datetime.time,
    ramp_up_minutes: int,
    weekdays: int,
) -> bytearray:
    """Create a delete auto setting command."""
    return create_add_auto_setting_command(msg_id, sunrise, sunset, (255, 255, 255), ramp_up_minutes, weekdays)


def create_reset_auto_settings_command(msg_id: tuple[int, int]) -> bytearray:
    """Create a reset auto settings command."""
    return create_command_encoding(90, 5, msg_id, [5, 255, 255])


def create_switch_to_auto_mode_command(msg_id: tuple[int, int]) -> bytearray:
    """Create a switch to auto mode command."""
    return create_command_encoding(90, 5, msg_id, [18, 255, 255])
