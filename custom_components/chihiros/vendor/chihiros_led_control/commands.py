"""High-level Chihiros command builders."""

from __future__ import annotations

import datetime
from collections.abc import Sequence

from .protocol import create_command_encoding, encode_timestamp

AUTO_SETTING_PARAMETER_COUNT = 14
AUTO_SETTING_METADATA_PARAMETER_COUNT = 6


def create_base_auth_command(msg_id: tuple[int, int]) -> bytearray:
    """Create the base LED auth/status command used at connection startup."""
    return create_command_encoding(90, 4, msg_id, [1])


def create_set_time_command(msg_id: tuple[int, int]) -> bytearray:
    """Create the current time command."""
    return create_command_encoding(90, 9, msg_id, encode_timestamp(datetime.datetime.now()))


def create_set_brightness_command(msg_id: tuple[int, int], color: int, brightness_level: int) -> bytearray:
    """Create a brightness command."""
    return create_command_encoding(90, 7, msg_id, [color, brightness_level])


def create_query_status_command(msg_id: tuple[int, int]) -> bytearray:
    """Create a command that asks legacy LED devices for runtime/status notifications."""
    return create_base_auth_command(msg_id)


def create_add_auto_setting_command(
    msg_id: tuple[int, int],
    sunrise: datetime.time,
    sunset: datetime.time,
    brightness: Sequence[int],
    ramp_up_minutes: int,
    weekdays: int,
) -> bytearray:
    """Create an add auto setting command."""
    if len(brightness) > AUTO_SETTING_PARAMETER_COUNT - AUTO_SETTING_METADATA_PARAMETER_COUNT:
        raise ValueError("Auto setting brightness has too many channel values")

    parameters = [
        sunrise.hour,
        sunrise.minute,
        sunset.hour,
        sunset.minute,
        ramp_up_minutes,
        weekdays,
        *brightness,
    ]
    parameters.extend([255] * (AUTO_SETTING_PARAMETER_COUNT - len(parameters)))

    return create_command_encoding(165, 25, msg_id, parameters)


def create_delete_auto_setting_command(
    msg_id: tuple[int, int],
    sunrise: datetime.time,
    sunset: datetime.time,
    ramp_up_minutes: int,
    weekdays: int,
    brightness_channels: int = 3,
) -> bytearray:
    """Create a delete auto setting command."""
    return create_add_auto_setting_command(
        msg_id,
        sunrise,
        sunset,
        [255] * brightness_channels,
        ramp_up_minutes,
        weekdays,
    )


def create_reset_auto_settings_command(msg_id: tuple[int, int]) -> bytearray:
    """Create a reset auto settings command."""
    return create_command_encoding(90, 5, msg_id, [5, 255, 255])


def create_switch_to_auto_mode_command(msg_id: tuple[int, int]) -> bytearray:
    """Create a switch to auto mode command."""
    return create_command_encoding(90, 5, msg_id, [18, 255, 255])
