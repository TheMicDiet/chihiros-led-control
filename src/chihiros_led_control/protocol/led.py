"""LED family commands and notification codec."""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence

from ..models import LedProtocol
from .frame import create_command_encoding, encode_timestamp
from .notifications import (
    FanStatusNotification,
    RuntimeNotification,
    SchedulePoint,
    ScheduleSnapshotNotification,
)

AUTO_SETTING_PARAMETER_COUNT = 14
AUTO_SETTING_METADATA_PARAMETER_COUNT = 6


def create_base_auth_command(msg_id: tuple[int, int]) -> bytearray:
    """Create the base LED auth/status command used at connection startup (app's getDeviceInfo())."""
    return create_command_encoding(90, 4, msg_id, [1])


def create_set_time_command(msg_id: tuple[int, int], timestamp: datetime.datetime | None = None) -> bytearray:
    """Create the current time command."""
    return create_command_encoding(90, 9, msg_id, encode_timestamp(timestamp or datetime.datetime.now()))


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


AUTO_POINT_MAX_MINUTES = 2880


def _validate_auto_point_parameters(channel: int, minutes: int, level: int) -> None:
    """Validate one auto-curve point payload."""
    if not 0 <= channel <= 7:
        raise ValueError("Channel must be between 0 and 7")
    if not 0 <= minutes <= AUTO_POINT_MAX_MINUTES:
        raise ValueError(f"Minutes must be between 0 and {AUTO_POINT_MAX_MINUTES}")
    if not 0 <= level <= 100:
        raise ValueError("Level must be between 0 and 100")


def _auto_point_parameters(channel: int, minutes: int, level: int, *, protocol: LedProtocol) -> list[int]:
    """Encode an auto-curve point for the selected LED protocol."""
    if protocol is LedProtocol.SEA_LED:
        hour, minute = divmod(minutes, 60)
        return [channel, hour, minute, level]
    time_index, remainder = divmod(minutes, 30)
    if remainder > 14:
        time_index += 1
    return [channel, time_index, level]


def create_auto_point_command(
    msg_id: tuple[int, int],
    channel: int,
    minutes: int,
    level: int,
    *,
    protocol: LedProtocol = LedProtocol.BLE_LED,
) -> bytearray:
    """Create one auto-curve point (``0x5A, 6``) for a Commander/LED device.

    Time encoding depends on the selected ``LedProtocol``: SeaLed devices use
    ``[channel, hour, minute, level]``; BleLed/NewBleLed devices use
    ``[channel, 30-min-slot, level]`` with the app's rounding rule.

    ``minutes`` is minutes since midnight (0..1439; up to
    :data:`AUTO_POINT_MAX_MINUTES` for cross-day curves), ``level`` is 0..100.
    Payload bytes are sent as-is — a level of 90 stays 0x5A (the app does not
    escape parameter bytes).
    """
    _validate_auto_point_parameters(channel, minutes, level)
    parameters = _auto_point_parameters(channel, minutes, level, protocol=protocol)
    return create_command_encoding(90, 6, msg_id, parameters, avoid_reserved_byte=False)


def create_switch_to_auto_mode_command(msg_id: tuple[int, int]) -> bytearray:
    """Create a switch to auto mode command.

    Sends the schedule-driven scene frame ``(0x5A, 5, [18, 255, 255])``; the
    app's other auto variant ``switchToAuto()`` uses ``[3, 255, 255]``.
    """
    return create_command_encoding(90, 5, msg_id, [18, 255, 255])


def create_switch_to_manual_mode_command(msg_id: tuple[int, int]) -> bytearray:
    """Create a switch to manual mode command (app's ``switchToManual()`` frame)."""
    return create_command_encoding(90, 5, msg_id, [11, 255, 255])


def create_set_fan_speed_command(msg_id: tuple[int, int], speed_percent: int) -> bytearray:
    """Create a fan speed command for fan-equipped LED devices such as the WRGB VIVID III."""
    if speed_percent < 0 or speed_percent > 100:
        raise ValueError("Fan speed must be between 0 and 100 percent")
    return create_command_encoding(90, 15, msg_id, [speed_percent])


def create_fan_auto_mode_command(msg_id: tuple[int, int]) -> bytearray:
    """Create a fan auto mode command.

    Matches the app's ``autoFan()`` frame ``(0x5A, 5, [0x11, 0xFF, 0xFF])``;
    the device then starts/stops the fan from its temperature thresholds.
    """
    return create_command_encoding(90, 5, msg_id, [0x11, 0xFF, 0xFF])


def create_vivid3_fan_start_stop_temp_command(
    msg_id: tuple[int, int],
    start_temp: int,
    stop_temp: int,
) -> bytearray:
    """Create a VIVID3 fan start/stop temperature command.

    Matches the app's ``vvd3FanStartStopTemp()`` frame ``(0xA5, 45, ...)`` with
    38/33 °C defaults (5 °C hysteresis). Temperatures are payload bytes sent
    verbatim (reserved-byte avoidance disabled).
    """
    if not 0 <= start_temp <= 255 or not 0 <= stop_temp <= 255:
        raise ValueError("Fan temperatures must be between 0 and 255")
    return create_command_encoding(165, 45, msg_id, [start_temp, stop_temp], avoid_reserved_byte=False)


def create_vivid3_temp_protect_command(msg_id: tuple[int, int], enabled: bool) -> bytearray:
    """Create a VIVID3 temperature-protection switch command.

    Matches the app's ``vvd3tempProtect()`` frame ``(0x5A, 5, [0x31|0x30, 0xFF, 0xFF])``:
    payload byte 0 is 49 (on) or 48 (off).
    """
    return create_command_encoding(90, 5, msg_id, [0x31 if enabled else 0x30, 0xFF, 0xFF])


def create_vivid3_bluetooth_led_command(msg_id: tuple[int, int], enabled: bool) -> bytearray:
    """Create a VIVID3 indicator-LED switch command.

    Matches the app's ``vvd3BluetoothLed()`` frame ``(0x5A, 5, [0x32|0x31, 0xFF, 0xFF])``:
    payload byte 0 is 50 (on) or 49 (off).
    """
    return create_command_encoding(90, 5, msg_id, [0x32 if enabled else 0x31, 0xFF, 0xFF])


SCHEDULE_POINT_SIZE = 3
SCHEDULE_SNAPSHOT_POINTS_START = 25


def _notification_channels(color_channels: Mapping[str, int]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(color_channels.items(), key=lambda color_channel: color_channel[1]))


def parse_notification(data: bytes | bytearray, color_channels: Mapping[str, int] | None = None):
    """Parse LED/accessory notifications, or return ``None``."""
    if len(data) < 7 or data[0] != 0x5B:
        return None
    firmware_version = data[1]
    mode = data[5]
    if mode == 0x0A:
        return _parse_runtime_notification(data, firmware_version)
    if mode == 0x0B:
        return _parse_fan_notification(data, firmware_version)
    if mode != 0xFE or color_channels is None:
        return None
    return _parse_schedule_notification(data, firmware_version, color_channels)


def _parse_runtime_notification(data: bytes | bytearray, firmware_version: int):
    """Parse a runtime/status notification payload."""
    if len(data) < 8:
        return None
    return RuntimeNotification(firmware_version, (data[6] << 8) | data[7], bytes(data))


def _parse_fan_notification(data: bytes | bytearray, firmware_version: int):
    """Parse a fan status notification payload."""
    if len(data) < 9:
        return None
    return FanStatusNotification(firmware_version, (data[6] << 8) | data[7], data[8], bytes(data))


def _parse_schedule_notification(
    data: bytes | bytearray,
    firmware_version: int,
    color_channels: Mapping[str, int],
):
    """Parse the points in a schedule snapshot notification."""
    channels = _notification_channels(color_channels)
    points = []
    for index in range(SCHEDULE_SNAPSHOT_POINTS_START, len(data), SCHEDULE_POINT_SIZE):
        point = data[index : index + SCHEDULE_POINT_SIZE]
        if len(point) < SCHEDULE_POINT_SIZE:
            break
        hour, minute, level = point
        levels = {color: level for color, _channel_id in channels}
        if hour > 23 or minute > 59 or level > 100:
            continue
        if hour == 0 and minute == 0 and not any(value != 0 for value in levels.values()):
            continue
        points.append(SchedulePoint(hour, minute, levels))
    return ScheduleSnapshotNotification(firmware_version, tuple(points), bytes(data))


__all__ = [
    "AUTO_SETTING_PARAMETER_COUNT",
    "AUTO_SETTING_METADATA_PARAMETER_COUNT",
    "AUTO_POINT_MAX_MINUTES",
    "create_base_auth_command",
    "create_set_time_command",
    "create_set_brightness_command",
    "create_query_status_command",
    "create_add_auto_setting_command",
    "create_delete_auto_setting_command",
    "create_reset_auto_settings_command",
    "create_auto_point_command",
    "create_switch_to_auto_mode_command",
    "create_switch_to_manual_mode_command",
    "create_set_fan_speed_command",
    "create_fan_auto_mode_command",
    "create_vivid3_fan_start_stop_temp_command",
    "create_vivid3_temp_protect_command",
    "create_vivid3_bluetooth_led_command",
    "parse_notification",
    "FanStatusNotification",
    "RuntimeNotification",
    "SchedulePoint",
    "ScheduleSnapshotNotification",
]
