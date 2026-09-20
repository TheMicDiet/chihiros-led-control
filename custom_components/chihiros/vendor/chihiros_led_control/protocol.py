"""BLE protocol helpers for Chihiros commands."""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

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

    Dosing pumps notify with header 0x5B and mode 0x1E. Each channel is a
    big-endian 16-bit counter scaled to microliters: ``(hi << 8 | lo) * 100``.
    """

    total_dosed_ul: tuple[int, ...]
    raw: bytes = field(default=b"", compare=False)


@dataclass(frozen=True)
class DosingDailyNotification:
    """Per-channel "dosed today" volumes reported by a dosing pump.

    Dosing pumps notify with header 0x5B and mode 0x22. Each channel is a
    big-endian 16-bit counter scaled to microliters: ``(hi << 8 | lo) * 100``.
    """

    dose_use_in_day_ul: tuple[int, ...]
    raw: bytes = field(default=b"", compare=False)


@dataclass(frozen=True)
class HeaterTemperatureNotification:
    """Temperatures pushed by a Chihiros heater.

    Heaters notify with header 0x5B and mode 0x25. Both temperatures are
    big-endian 16-bit tenths of a degree: the setting temperature at bytes
    6..7 and the measured (current) temperature at bytes 10..11
    (``chihiros_xapk/HEATER_CONTROL.md`` §4.1).
    """

    setting_temperature_celsius: float
    current_temperature_celsius: float
    raw: bytes = field(default=b"", compare=False)


@dataclass(frozen=True)
class HeaterStatusNotification:
    """Runtime/alarm status pushed by a Chihiros heater.

    Heaters notify with header 0x5B, mode 0x0A and a fixed 16-byte frame —
    the mode byte is shared with the LED runtime frame, so the length is what
    tells them apart. ``work_time_hours`` counts heating runtime since the last
    cleaning reset (the app warns to clean past 2160 h) and ``alarms`` is the
    raw ``data[14]`` bitfield (see :data:`HEATER_ALARM_BITS`)
    (``chihiros_xapk/HEATER_CONTROL.md`` §4.2/§4.3).
    """

    firmware_version: int
    work_time_hours: int
    alarms: int
    raw: bytes = field(default=b"", compare=False)


# Heater alarm flags in status-frame byte 14, mapped to stable names. The app
# tests bits 0-6 one by one; the mapping is live-verified against the vendor
# app (``chihiros_xapk/HEATER_CONTROL.md`` §4.3).
HEATER_ALARM_BITS: Mapping[str, int] = MappingProxyType(
    {
        "insufficient_water": 0x01,
        "power_too_low": 0x02,
        "water_overheat": 0x04,
        "needs_cleaning": 0x08,
        "exceeds_protection_temperature": 0x10,
        "heating_failure": 0x20,
        "sensor_failure": 0x40,
    }
)


def heater_alarm_names(alarms: int) -> tuple[str, ...]:
    """Return the names of the alarm flags set in a heater status bitfield."""
    return tuple(name for name, bit in HEATER_ALARM_BITS.items() if alarms & bit)


ParsedNotification = (
    RuntimeNotification
    | FanStatusNotification
    | ScheduleSnapshotNotification
    | DosingTotalsNotification
    | DosingDailyNotification
    | HeaterTemperatureNotification
    | HeaterStatusNotification
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
    *,
    heater: bool = False,
) -> ParsedNotification | None:
    """Parse known Chihiros notification payloads.

    ``heater`` selects the heater frame layouts: its status frame reuses the
    LED runtime mode byte (``0x0A``) with different fields, so the device
    family must be known to decode it.
    """
    # Notification framing differs between device generations. Some devices do
    # not provide a reliable declared length or trailing checksum, so parse the
    # known header and mode fields defensively instead of rejecting the entire
    # diagnostic payload.
    if len(data) < 7:
        return None

    mode = data[5]
    if data[0] == 0x5B:
        if heater:
            return _parse_heater_notification(data, mode)
        return _parse_legacy_notification(data, mode, color_channels)
    return None


def _parse_heater_notification(
    data: bytes | bytearray,
    mode: int,
) -> ParsedNotification | None:
    """Parse 0x5B heater temperature (0x25) and status (0x0A) frames."""
    if mode == 0x25 and len(data) >= 12:
        return HeaterTemperatureNotification(
            setting_temperature_celsius=((data[6] << 8) | data[7]) / 10,
            current_temperature_celsius=((data[10] << 8) | data[11]) / 10,
            raw=bytes(data),
        )
    if mode == 0x0A and len(data) == 16:
        return HeaterStatusNotification(
            firmware_version=(data[11] << 8) | data[12],
            work_time_hours=(data[7] << 8) | data[8],
            alarms=data[14],
            raw=bytes(data),
        )
    return None


def _parse_legacy_dosing_reply(
    data: bytes | bytearray,
    mode: int,
) -> ParsedNotification | None:
    """Parse 0x5B dose-counter replies from dosing pumps.

    Some captured DYDOSE firmware (fw ``07.25.18``) answers the
    ``(0xA5, 4, [4])`` / ``([5])`` pulls with ``0x5B`` uplink frames — modes
    ``0x1E`` (lifetime) and ``0x22`` (today). The app's ``dosing_state_widget``
    compares those same bytes (its disassembly immediates ``#0x3c``/``#0x44``
    are Dart smis, i.e. ``0x1E``/``0x22``). The trailing checksum byte sits
    outside the channel region.
    """
    if mode == 0x1E and len(data) >= 8:
        return DosingTotalsNotification(_parse_dosing_channel_values(data), bytes(data))
    if mode == 0x22 and len(data) >= 8:
        return DosingDailyNotification(_parse_dosing_channel_values(data), bytes(data))
    return None


def _parse_legacy_notification(
    data: bytes | bytearray,
    mode: int,
    color_channels: Mapping[str, int] | None,
) -> ParsedNotification | None:
    """Parse 0x5B legacy LED/accessory notification frames."""
    firmware_version = data[1]
    if mode == 0x0A:
        return _parse_runtime_notification(data, firmware_version)
    if mode == 0x0B:
        return _parse_fan_status_notification(data, firmware_version)
    if mode in (0x1E, 0x22):
        return _parse_legacy_dosing_reply(data, mode)
    if mode == 0xFE:
        return _parse_schedule_snapshot(data, firmware_version, color_channels)
    return None


def _parse_runtime_notification(data: bytes | bytearray, firmware_version: int) -> ParsedNotification | None:
    """Parse 0x5B mode 0x0A runtime/status frames."""
    if len(data) < 8:
        return None
    runtime_minutes = (data[6] << 8) | data[7]
    return RuntimeNotification(firmware_version, runtime_minutes, bytes(data))


def _parse_fan_status_notification(data: bytes | bytearray, firmware_version: int) -> ParsedNotification | None:
    """Parse 0x5B mode 0x0B fan status frames.

    This is the fan RPM/temperature readout (the app's ``vvd3_fan_widget``
    compares the smi immediates ``#0xb6``/``#0x16`` = ``0x5B``/``0x0B``):
    ``rpm = (data[6] << 8) | data[7]``, ``temperature = data[8]``.
    """
    if len(data) < 9:
        return None
    fan_rpm = (data[6] << 8) | data[7]
    temperature_celsius = data[8]
    return FanStatusNotification(firmware_version, fan_rpm, temperature_celsius, bytes(data))


def _parse_schedule_snapshot(
    data: bytes | bytearray,
    firmware_version: int,
    color_channels: Mapping[str, int] | None,
) -> ParsedNotification | None:
    """Parse 0x5B mode 0xFE schedule snapshot frames."""
    if color_channels is None:
        return None
    channels = _notification_channels(color_channels)
    points = _parse_schedule_points(data, channels)
    return ScheduleSnapshotNotification(firmware_version, points, bytes(data))


def _parse_schedule_points(
    data: bytes | bytearray,
    channels: tuple[tuple[str, int], ...],
) -> tuple[SchedulePoint, ...]:
    """Decode the trailing schedule point payload of a snapshot frame."""
    points: list[SchedulePoint] = []
    for index in range(SCHEDULE_SNAPSHOT_POINTS_START, len(data), SCHEDULE_POINT_SIZE):
        point = data[index : index + SCHEDULE_POINT_SIZE]
        if len(point) < SCHEDULE_POINT_SIZE:
            break
        hour, minute, level = point
        levels = {color: level for color, _channel_id in channels}
        if _is_valid_schedule_point(hour, minute, level, levels):
            points.append(SchedulePoint(hour, minute, levels))
    return tuple(points)


def _is_valid_schedule_point(hour: int, minute: int, level: int, levels: Mapping[str, int]) -> bool:
    """Return whether a raw snapshot point should be kept.

    Points outside the valid hour/minute/level ranges are dropped, as is the
    all-channel-off midnight placeholder the devices report.
    """
    if hour > 23 or minute > 59 or level > 100:
        return False
    if hour == 0 and minute == 0:
        return any(level_value != 0 for level_value in levels.values())
    return True
