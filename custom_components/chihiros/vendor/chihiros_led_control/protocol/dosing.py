"""Dosing-pump family commands and notification codec."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum

from .frame import create_command_encoding
from .notifications import DosingDailyNotification, DosingTotalsNotification

AUTO_SETTING_PARAMETER_COUNT = 14
AUTO_SETTING_METADATA_PARAMETER_COUNT = 6
DOSE_VOLUME_BUCKET_TENTHS_ML = 256
DOSE_VOLUME_MAX_ML = 6553.5
MANUAL_DOSE_VOLUME_MIN_ML = 0.2
MANUAL_DOSE_VOLUME_MAX_ML = 999.9
DOSING_SCHEDULE_MAX_PAYLOAD = 50


class DosingMode(IntEnum):
    """Dosing-pump schedule modes; the enum index is the wire mode byte."""

    SINGLE = 0
    AUTO = 1
    FREE = 2
    TIMER = 3


@dataclass(frozen=True)
class DosingWorkPoint:
    """One schedule work point (app's ``DosingWorkPoint``).

    ``volume_ml`` is the per-dose volume for single/auto/timer modes. For free
    mode the point instead spans a window: ``duration_minutes`` is the window
    length and ``number`` the dose count inside it (free mode carries no
    volume — the pump splits the daily total itself).

    The defaults match the app's ``DosingWorkPoint`` constructor
    (``chihiros_xapk/DOSING_CONTROL.md`` §4.3; binary-verified at 0x92db50:
    ``number = 2`` stored to field_47 @ 0x92dbe8 and
    ``duration_minutes = 120`` to field_3f @ 0x92dbb0, both unboxed ints).
    """

    start_hour: int
    start_minute: int
    volume_ml: float = 0.0
    duration_minutes: int = 120
    number: int = 2


def split_dose_volume_ml(ml: float) -> tuple[int, int]:
    """Encode a manual dosing volume as 25.6 mL buckets plus 0.1 mL remainder."""
    if ml < MANUAL_DOSE_VOLUME_MIN_ML or ml > MANUAL_DOSE_VOLUME_MAX_ML:
        raise ValueError(f"Dose volume must be between {MANUAL_DOSE_VOLUME_MIN_ML} and {MANUAL_DOSE_VOLUME_MAX_ML} mL")
    tenths_ml = int(round(ml * 10))
    return divmod(tenths_ml, DOSE_VOLUME_BUCKET_TENTHS_ML)


def encode_dose_volume_ml(ml: float) -> tuple[int, int]:
    """Encode a volume in mL as the 0.1 mL wire bytes shared by dosing frames.

    The app's ``_volumeChange`` maps integer microliters to
    ``[(vol ~/ 100) >> 8, (vol ~/ 100) & 0xFF]`` (truncating division), so
    sub-0.1 mL remainders are floored: 105.55 mL encodes as ``(4, 31)``.
    Unlike :func:`split_dose_volume_ml` the full two-byte range is usable
    (0 to 6553.5 mL) so daily-dose and stirrer workloads encode faithfully.
    """
    if ml < 0 or ml > DOSE_VOLUME_MAX_ML:
        raise ValueError(f"Volume must be between 0 and {DOSE_VOLUME_MAX_ML} mL")
    microliters = round(ml * 1000)
    tenths_ml = microliters // 100
    return divmod(tenths_ml, DOSE_VOLUME_BUCKET_TENTHS_ML)


def _validate_dosing_channel(channel: int) -> None:
    """Validate a zero-based dosing/stirrer channel index."""
    if channel < 0 or channel > 7:
        raise ValueError("Channel must be between 0 and 7")


def create_dosing_set_command(
    msg_id: tuple[int, int],
    channel: int,
    dose_per_day_ml: float | None,
    frequency: int,
    *,
    is_first_setting: bool,
) -> bytearray:
    """Create the app's ``dosingSet`` frame ``(0xA5, 27)``.

    Payload ``[channel, frequency, 1, first?0:1, vol_hi, vol_lo]``: the fixed
    byte 2 is 1 on the wire and the first-setting byte is polarity-inverted
    (0 when this *is* the channel's first programming of the day). A
    ``dose_per_day_ml`` of ``None`` encodes the app's null volume
    ``[255, 255]``; the stirrer programs daily volume 0 as ``[0, 0]``.
    """
    _validate_dosing_channel(channel)
    if not 0 <= frequency <= 255:
        raise ValueError("Frequency must be between 0 and 255")
    volume = [255, 255] if dose_per_day_ml is None else list(encode_dose_volume_ml(dose_per_day_ml))
    parameters = [channel, frequency, 1, 0 if is_first_setting else 1, *volume]
    return create_command_encoding(165, 27, msg_id, parameters, avoid_reserved_byte=False)


def _validate_work_point_time(point: DosingWorkPoint) -> None:
    """Validate the start time of a schedule work point."""
    if not 0 <= point.start_hour <= 23 or not 0 <= point.start_minute <= 59:
        raise ValueError("Work point start time must be a valid wall-clock time")


def _single_volume_record(point: DosingWorkPoint) -> list[int]:
    """Encode one single/auto/timer point as ``[hour, minute, vol_hi, vol_lo]``."""
    _validate_work_point_time(point)
    return [point.start_hour, point.start_minute, *encode_dose_volume_ml(point.volume_ml)]


def _free_mode_record(point: DosingWorkPoint) -> list[int]:
    """Encode one free-mode point as ``[sh, sm, eh, em, number]`` (no volume)."""
    _validate_work_point_time(point)
    if point.duration_minutes < 1:
        raise ValueError("Free-mode work points need a duration of at least 1 minute")
    if not 0 <= point.number <= 255:
        raise ValueError("Free-mode dose count must be between 0 and 255")
    total_minute = point.start_minute + point.duration_minutes
    end_hour = point.start_hour + total_minute // 60
    end_minute = total_minute % 60
    return [point.start_hour, point.start_minute, end_hour, end_minute, point.number]


def _flush_batched_frames(
    msg_id: tuple[int, int],
    cmd_mode: int,
    header: list[int],
    records: Sequence[Sequence[int]],
) -> list[bytearray]:
    """Batch schedule records into frames like the app's accumulator.

    Mirrors ``dosingWorkNew`` exactly: records are appended first and the
    accumulator is flushed only once its payload *exceeds* 50 bytes
    (``cmp #0x32``), then reset to the header. Frames can therefore carry up
    to 54 payload bytes (header + 13 timer records), matching the app's frame
    splits byte-for-byte.
    """
    frames: list[bytearray] = []
    accumulator = list(header)
    for record in records:
        accumulator.extend(record)
        if len(accumulator) > DOSING_SCHEDULE_MAX_PAYLOAD:
            frames.append(create_command_encoding(165, cmd_mode, msg_id, accumulator, avoid_reserved_byte=False))
            accumulator = list(header)
    if accumulator != header:
        frames.append(create_command_encoding(165, cmd_mode, msg_id, accumulator, avoid_reserved_byte=False))
    return frames


def create_dosing_schedule_command(
    msg_id: tuple[int, int],
    channel: int,
    mode: DosingMode,
    points: Sequence[DosingWorkPoint],
) -> list[bytearray]:
    """Create the app's ``dosingWorkNew`` frames for one channel.

    Single and auto modes emit one ``(0xA5, 21)`` frame per point; timer mode
    batches ``[channel, 3] + [hour, minute, vol_hi, vol_lo] * N`` and free
    mode batches ``[channel] + [sh, sm, eh, em, number] * N`` into ``(0xA5,
    21)`` / ``(0xA5, 23)`` frames capped at 50 payload bytes.
    """
    _validate_dosing_channel(channel)
    if not points:
        raise ValueError("At least one work point is required")
    if mode in (DosingMode.SINGLE, DosingMode.AUTO):
        return [
            create_command_encoding(
                165, 21, msg_id, [channel, int(mode), *_single_volume_record(point)], avoid_reserved_byte=False
            )
            for point in points
        ]
    if mode is DosingMode.TIMER:
        records = [_single_volume_record(point) for point in points]
        return _flush_batched_frames(msg_id, 21, [channel, int(mode)], records)
    records = [_free_mode_record(point) for point in points]
    return _flush_batched_frames(msg_id, 23, [channel], records)


def create_reset_dosing_channel_command(msg_id: tuple[int, int], channel: int) -> bytearray:
    """Create the app's ``resetDosingChannel`` frame ``(0xA5, 5, [ch+25, 255, 255])``."""
    _validate_dosing_channel(channel)
    return create_command_encoding(165, 5, msg_id, [channel + 25, 255, 255], avoid_reserved_byte=False)


def create_reset_total_dosing_command(msg_id: tuple[int, int], channel: int) -> bytearray:
    """Create the app's ``resetTotalDosing`` frame ``(0xA5, 5, [ch+21, 255, 255])``.

    Sent when the user sets a container volume; it zeroes the pump's lifetime
    counter for the channel.
    """
    _validate_dosing_channel(channel)
    return create_command_encoding(165, 5, msg_id, [channel + 21, 255, 255], avoid_reserved_byte=False)


def create_dosing_calibrate_command(
    msg_id: tuple[int, int],
    channel: int,
    seconds: int | None = None,
    volume_ml: float | None = None,
) -> bytearray:
    """Create the app's ``dosingCalibrate`` frame ``(0xA5, 22)``.

    Payload ``[channel, time?, vol_int, vol_frac]``: ``seconds`` is the test
    dose run time (0-254; 255 marks the field as omitted) and the volume
    splits as ``[int mL, 2-digit fraction]`` (255/255 when omitted) — e.g.
    2.5 mL encodes as ``(2, 50)``. The fraction byte is rounded half-up like
    the app (``LibcRound`` @ 0xa69398) and can be ``100``, which the device
    reads as the next whole mL (2.999 mL encodes as ``(2, 100)``).
    """
    _validate_dosing_channel(channel)
    if seconds is None:
        time_byte = 255
    elif 0 <= seconds <= 254:  # 255 is reserved for "omitted" on the wire
        time_byte = seconds
    else:
        raise ValueError("Calibration seconds must be between 0 and 254 (255 means omitted)")
    if volume_ml is None:
        volume = [255, 255]
    else:
        microliters = round(volume_ml * 1000)
        whole_ml, remainder_ul = divmod(microliters, 1000)
        if not 0 <= whole_ml <= 255:
            raise ValueError("Calibration volume must be between 0 and 255.99 mL")
        # Round the 2-digit fraction half-up like the app (LibcRound @
        # 0xa69398); the result may be 100 = the next whole mL.
        volume = [whole_ml, (remainder_ul + 5) // 10]
    return create_command_encoding(165, 22, msg_id, [channel, time_byte, *volume], avoid_reserved_byte=False)


def create_set_dosing_delay_command(msg_id: tuple[int, int], enabled: bool) -> bytearray:
    """Create the app's ``setDosingDelay`` frame ``(0xA5, 31, [enabled?1:0])``."""
    return create_command_encoding(165, 31, msg_id, [1 if enabled else 0], avoid_reserved_byte=False)


def create_dosing_active_compensation_command(
    msg_id: tuple[int, int],
    channel: int,
    *,
    active: bool,
    compensate: bool,
) -> bytearray:
    """Create the app's ``setDosingInterruptCompensationAndActive`` frame ``(0xA5, 32)``.

    Payload ``[channel, compensate?1:0, active?1:0]``; the app sends it before
    every ``dosingSet``/schedule write.
    """
    _validate_dosing_channel(channel)
    parameters = [channel, 1 if compensate else 0, 1 if active else 0]
    return create_command_encoding(165, 32, msg_id, parameters, avoid_reserved_byte=False)


def create_dosing_channel_color_command(msg_id: tuple[int, int], channel: int, color: int) -> bytearray:
    """Create the new-generation pump's ``dosingChannelColor`` frame ``(0xA5, 59)``."""
    _validate_dosing_channel(channel)
    if not 0 <= color <= 255:
        raise ValueError("Color must be between 0 and 255")
    return create_command_encoding(165, 59, msg_id, [channel, color], avoid_reserved_byte=False)


def create_general_temp_run_command(
    msg_id: tuple[int, int],
    states: Mapping[int, bool],
    seconds: int | None = None,
) -> bytearray:
    """Create the shared ``generalTempSet`` "temporary run" frame ``(0xA5, 20)``.

    Payload ``[duration_min][duration_sec][8 channel bytes]``; the duration
    bytes come FIRST (binary-verified against My Chihiros 2.8.59:
    generalTempSet builds ``[min, sec]`` then ``addAll`` the channel bytes at
    0x91fdb8/0x920030). Channel bytes default to 255 and are overlaid with
    1 (run) / 0 (stop) per the ``states`` mapping. A ``seconds`` of ``None``
    encodes the unlimited duration ``[255, 255]``. Note: the app only ever
    overlays the first 4 channel bytes; addressing channels 4-7 here is an
    unobserved generalization.
    """
    channel_bytes = [255] * 8
    for channel, run in states.items():
        _validate_dosing_channel(channel)
        channel_bytes[channel] = 1 if run else 0
    if seconds is None:
        duration = [255, 255]
    else:
        if not 0 <= seconds <= 255 * 60 + 59:
            raise ValueError("Duration must be between 0 and 15359 seconds")
        minutes, remainder = divmod(seconds, 60)
        duration = [minutes, remainder]
    return create_command_encoding(165, 20, msg_id, [*duration, *channel_bytes], avoid_reserved_byte=False)


def create_dose_auth_1_command(msg_id: tuple[int, int]) -> bytearray:
    """Create the first dosing pump auth command."""
    return create_command_encoding(165, 4, msg_id, [4])


def create_dose_auth_2_command(msg_id: tuple[int, int]) -> bytearray:
    """Create the second dosing pump auth command."""
    return create_command_encoding(165, 4, msg_id, [5])


def create_manual_dose_command(msg_id: tuple[int, int], pump_idx: int, volume_ml: float) -> bytearray:
    """Create a manual dosing command for one pump.

    Volumes are encoded as ``high * 25.6 mL + low * 0.1 mL``. This is compatible
    with the older single-byte examples for doses up to 25.5 mL because
    ``high`` is then zero. Dosing pumps expose up to eight channels.
    """
    if pump_idx < 0 or pump_idx > 7:
        raise ValueError("Pump index must be between 0 and 7")
    high, low = split_dose_volume_ml(volume_ml)
    return create_command_encoding(165, 27, msg_id, [pump_idx, 0, 0, high, low], avoid_reserved_byte=False)


def parse_notification(data: bytes | bytearray):
    """Parse dosing-pump counter notifications, or return ``None``."""
    if len(data) < 8 or data[0] != 0x5B:
        return None
    values = _parse_channel_values(data)
    if data[5] == 0x1E:
        return DosingTotalsNotification(values, bytes(data))
    if data[5] == 0x22:
        return DosingDailyNotification(values, bytes(data))
    return None


def _parse_channel_values(data: bytes | bytearray) -> tuple[int, ...]:
    """Decode big-endian channel counters scaled to microlitres."""
    channel_count = (len(data) - 6) // 2
    return tuple(((data[6 + 2 * index] << 8) | data[7 + 2 * index]) * 100 for index in range(channel_count))


__all__ = [
    "DOSE_VOLUME_BUCKET_TENTHS_ML",
    "DOSE_VOLUME_MAX_ML",
    "MANUAL_DOSE_VOLUME_MIN_ML",
    "MANUAL_DOSE_VOLUME_MAX_ML",
    "DOSING_SCHEDULE_MAX_PAYLOAD",
    "DosingMode",
    "DosingWorkPoint",
    "split_dose_volume_ml",
    "encode_dose_volume_ml",
    "create_dosing_set_command",
    "create_dosing_schedule_command",
    "create_reset_dosing_channel_command",
    "create_reset_total_dosing_command",
    "create_dosing_calibrate_command",
    "create_set_dosing_delay_command",
    "create_dosing_active_compensation_command",
    "create_dosing_channel_color_command",
    "create_general_temp_run_command",
    "create_dose_auth_1_command",
    "create_dose_auth_2_command",
    "create_manual_dose_command",
    "parse_notification",
]
