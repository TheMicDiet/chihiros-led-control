"""BLE protocol helpers for Chihiros commands."""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from dataclasses import dataclass

RESERVED_BYTE = 0x5A
SCHEDULE_POINT_TIME_BYTES = 2


@dataclass(frozen=True)
class RuntimeNotification:
    """Parsed runtime/status notification."""

    firmware_version: int
    runtime_minutes: int


@dataclass(frozen=True)
class SchedulePoint:
    """Parsed auto schedule point."""

    hour: int
    minute: int
    levels: Mapping[str, int]


@dataclass(frozen=True)
class ScheduleSnapshotNotification:
    """Parsed auto schedule/status snapshot notification."""

    firmware_version: int
    points: tuple[SchedulePoint, ...]


ParsedNotification = RuntimeNotification | ScheduleSnapshotNotification


def next_message_id(current_msg_id: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    """Generate the next Bluetooth message id."""
    msg_id_higher_byte, msg_id_lower_byte = current_msg_id
    while True:
        if msg_id_higher_byte == 255 and msg_id_lower_byte == 255:
            msg_id_higher_byte, msg_id_lower_byte = 0, 1
        elif msg_id_lower_byte == 255:
            msg_id_higher_byte = (msg_id_higher_byte + 1) % 256
            msg_id_lower_byte = 0
        else:
            msg_id_lower_byte += 1

        if msg_id_higher_byte != RESERVED_BYTE and msg_id_lower_byte != RESERVED_BYTE:
            return (msg_id_higher_byte, msg_id_lower_byte)


def calculate_checksum(input_bytes: bytes | bytearray) -> int:
    """Calculate the command checksum."""
    if len(input_bytes) < 7:
        raise ValueError("Commands must contain at least 7 bytes")
    checksum = input_bytes[1]
    for input_byte in input_bytes[2:]:
        checksum = checksum ^ input_byte
    return checksum


def normalize_message_id(msg_id: tuple[int, int], *, avoid_reserved_byte: bool = True) -> tuple[int, int]:
    """Return a message ID that is safe for the selected protocol variant."""
    if not avoid_reserved_byte:
        return msg_id
    if msg_id[0] == RESERVED_BYTE or msg_id[1] == RESERVED_BYTE:
        return next_message_id(msg_id)
    return msg_id


def create_command_encoding(
    cmd_id: int,
    cmd_mode: int,
    msg_id: tuple[int, int],
    parameters: list[int],
    *,
    avoid_reserved_byte: bool = True,
) -> bytearray:
    """Encode a Chihiros BLE command."""
    safe_msg_id = normalize_message_id(msg_id, avoid_reserved_byte=avoid_reserved_byte)
    sanitized_params = [
        value if not avoid_reserved_byte or value != RESERVED_BYTE else RESERVED_BYTE - 1 for value in parameters
    ]
    command = bytearray(
        [cmd_id, 1, len(sanitized_params) + 5, safe_msg_id[0], safe_msg_id[1], cmd_mode] + sanitized_params
    )

    verification_byte = calculate_checksum(command)
    if avoid_reserved_byte and verification_byte == RESERVED_BYTE:
        return create_command_encoding(
            cmd_id,
            cmd_mode,
            next_message_id(safe_msg_id),
            sanitized_params,
            avoid_reserved_byte=avoid_reserved_byte,
        )

    return command + bytes([verification_byte])


def encode_timestamp(ts: datetime.datetime) -> list[int]:
    """Encode a timestamp as Chihiros command parameters."""
    return [ts.year - 2000, ts.month, ts.isoweekday(), ts.hour, ts.minute, ts.second]


def _notification_channels(color_channels: Mapping[str, int]) -> tuple[tuple[str, int], ...]:
    """Return notification channels sorted by protocol channel id."""
    return tuple(sorted(color_channels.items(), key=lambda color_channel: color_channel[1]))


def parse_notification(
    data: bytes | bytearray,
    color_channels: Mapping[str, int] | None = None,
) -> ParsedNotification | None:
    """Parse known Chihiros notification payloads."""
    if len(data) < 7 or data[0] != 0x5B:
        return None

    firmware_version = data[1]
    mode = data[5]
    if mode == 0x0A and len(data) >= 8:
        runtime_minutes = (data[6] << 8) | data[7]
        return RuntimeNotification(firmware_version, runtime_minutes)

    if mode == 0xFE:
        if color_channels is None:
            return None
        channels = _notification_channels(color_channels)
        point_size = SCHEDULE_POINT_TIME_BYTES + len(channels)
        points: list[SchedulePoint] = []
        for index in range(6, len(data), point_size):
            point = data[index : index + point_size]
            if len(point) < point_size:
                break
            hour = point[0]
            minute = point[1]
            levels = dict(zip((color for color, _channel_id in channels), point[2:], strict=True))
            if hour > 23 or minute > 59 or any(level > 100 for level in levels.values()):
                continue
            if hour == 0 and minute == 0 and all(level == 0 for level in levels.values()):
                continue
            points.append(SchedulePoint(hour, minute, levels))
        return ScheduleSnapshotNotification(firmware_version, tuple(points))

    return None
