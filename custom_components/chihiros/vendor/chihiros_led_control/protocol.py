"""BLE protocol helpers for Chihiros commands."""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from dataclasses import dataclass, field

RESERVED_BYTE = 0x5A
# Sequence bytes skip only 0x5A (the app's dataMaker.dart); 0x5B is the legacy
# notification header but remains a valid sequence byte.
RESERVED_MESSAGE_ID_BYTES = (RESERVED_BYTE,)
SCHEDULE_POINT_SIZE = 3
SCHEDULE_SNAPSHOT_POINTS_START = 25


@dataclass(frozen=True)
class RuntimeNotification:
    """Parsed runtime/status notification."""

    firmware_version: int
    runtime_minutes: int
    raw: bytes = field(default=b"", compare=False)


@dataclass(frozen=True)
class FanStatusNotification:
    """Parsed fan-equipped device status notification."""

    firmware_version: int
    fan_rpm: int
    temperature_celsius: int
    raw: bytes = field(default=b"", compare=False)


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
    raw: bytes = field(default=b"", compare=False)


@dataclass(frozen=True)
class DosingTotalsNotification:
    """Per-channel lifetime dosed volumes reported by a dosing pump.

    Dosing pumps notify with header 0xB6 and mode 0x3C. Each channel is a
    big-endian 16-bit counter scaled to microliters: ``(hi << 8 | lo) * 100``.
    """

    total_dosed_ul: tuple[int, ...]
    raw: bytes = field(default=b"", compare=False)


@dataclass(frozen=True)
class DosingDailyNotification:
    """Per-channel "dosed today" volumes reported by a dosing pump.

    Dosing pumps notify with header 0xB6 and mode 0x44. Each channel is a
    big-endian 16-bit counter scaled to microliters: ``(hi << 8 | lo) * 100``.
    """

    dose_use_in_day_ul: tuple[int, ...]
    raw: bytes = field(default=b"", compare=False)


@dataclass(frozen=True)
class Vivid3FanStatusNotification:
    """VIVID3 fan RPM/temperature readout notification.

    The vendor app reads fan RPM from header 0xB6 mode 0x16 frames with
    ``rpm = (data[6] << 8) | data[7]`` and ``temperature = data[8]``.
    """

    fan_rpm: int
    temperature_celsius: int
    raw: bytes = field(default=b"", compare=False)


ParsedNotification = (
    RuntimeNotification
    | FanStatusNotification
    | ScheduleSnapshotNotification
    | DosingTotalsNotification
    | DosingDailyNotification
    | Vivid3FanStatusNotification
)


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

        if msg_id_higher_byte not in RESERVED_MESSAGE_ID_BYTES and msg_id_lower_byte not in RESERVED_MESSAGE_ID_BYTES:
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
    if msg_id[0] in RESERVED_MESSAGE_ID_BYTES or msg_id[1] in RESERVED_MESSAGE_ID_BYTES:
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


def _parse_dosing_channel_values(data: bytes | bytearray) -> tuple[int, ...]:
    """Return per-channel 16-bit big-endian counters from a dosing notification.

    Channel ``i`` occupies bytes ``[6 + 2i, 7 + 2i]``; each counter is scaled
    to microliters with ``(hi << 8 | lo) * 100`` by the caller.
    """
    channel_count = (len(data) - 6) // 2
    return tuple(((data[6 + 2 * index] << 8) | data[7 + 2 * index]) * 100 for index in range(channel_count))


def parse_notification(
    data: bytes | bytearray,
    color_channels: Mapping[str, int] | None = None,
) -> ParsedNotification | None:
    """Parse known Chihiros notification payloads."""
    # Notification framing differs between device generations. Some devices do
    # not provide a reliable declared length or trailing checksum, so parse the
    # known header and mode fields defensively instead of rejecting the entire
    # diagnostic payload.
    if len(data) < 7:
        return None

    mode = data[5]
    if data[0] == 0x5B:
        return _parse_legacy_notification(data, mode, color_channels)
    if data[0] == 0xB6:
        return _parse_newer_notification(data, mode)
    return None


def _parse_legacy_notification(
    data: bytes | bytearray,
    mode: int,
    color_channels: Mapping[str, int] | None,
) -> ParsedNotification | None:
    """Parse 0x5B legacy LED/accessory notification frames."""
    firmware_version = data[1]
    if mode == 0x0A and len(data) >= 8:
        runtime_minutes = (data[6] << 8) | data[7]
        return RuntimeNotification(firmware_version, runtime_minutes, bytes(data))

    if mode == 0x0B and len(data) >= 9:
        fan_rpm = (data[6] << 8) | data[7]
        temperature_celsius = data[8]
        return FanStatusNotification(firmware_version, fan_rpm, temperature_celsius, bytes(data))

    if mode == 0xFE:
        if color_channels is None:
            return None
        channels = _notification_channels(color_channels)
        points: list[SchedulePoint] = []
        for index in range(SCHEDULE_SNAPSHOT_POINTS_START, len(data), SCHEDULE_POINT_SIZE):
            point = data[index : index + SCHEDULE_POINT_SIZE]
            if len(point) < SCHEDULE_POINT_SIZE:
                break
            hour, minute, level = point
            levels = {color: level for color, _channel_id in channels}
            if hour > 23 or minute > 59 or level > 100:
                continue
            if hour == 0 and minute == 0 and all(level == 0 for level in levels.values()):
                continue
            points.append(SchedulePoint(hour, minute, levels))
        return ScheduleSnapshotNotification(firmware_version, tuple(points), bytes(data))

    return None


def _parse_newer_notification(data: bytes | bytearray, mode: int) -> ParsedNotification | None:
    """Parse 0xB6 newer-generation notification frames (pumps, fan readouts)."""
    if mode == 0x3C and len(data) >= 8:
        return DosingTotalsNotification(_parse_dosing_channel_values(data), bytes(data))

    if mode == 0x44 and len(data) >= 8:
        return DosingDailyNotification(_parse_dosing_channel_values(data), bytes(data))

    if mode == 0x16 and len(data) >= 9:
        fan_rpm = (data[6] << 8) | data[7]
        temperature_celsius = data[8]
        return Vivid3FanStatusNotification(fan_rpm, temperature_celsius, bytes(data))

    return None
