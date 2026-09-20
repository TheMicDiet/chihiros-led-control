"""High-level Chihiros command builders."""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum

from .protocol import create_command_encoding, encode_timestamp

AUTO_SETTING_PARAMETER_COUNT = 14
AUTO_SETTING_METADATA_PARAMETER_COUNT = 6
DOSE_VOLUME_BUCKET_TENTHS_ML = 256

# Heater (DYHET/DYH1T) wire values, reverse-engineered from My Chihiros 2.8.59
# (``chihiros_xapk/HEATER_CONTROL.md``). Temperatures ride as the
# ``[whole, tenths]`` byte pair of ``round(temp * 10)`` and power as watts ÷ 10,
# which keeps the wire byte inside one byte across the app's range.
HEATER_MAX_POWER_WATTS = 2000
HEATER_MAX_TEMPERATURE_C = 100.0
# Vendor-app model defaults (``ChihirosHeater::init``): 25.0 °C setting
# temperature, 200 W manual power and a 37.0 °C overheat protector. The
# disassembly stores Dart smis, i.e. half the raw immediates quoted in
# ``HEATER_CONTROL.md`` §2.
HEATER_DEFAULT_TEMPERATURE_C = 25.0
HEATER_DEFAULT_POWER_WATTS = 200
HEATER_DEFAULT_PROTECTOR_TEMPERATURE_C = 37.0
# The same model keeps the auto-mode defaults that ride in the mode-43 frame
# with flag 1: the halved 40/1000 smis of §2, i.e. 20.0 °C at 500 W, matching
# the captured ``initAutoDefault`` frame.
HEATER_DEFAULT_AUTO_TEMPERATURE_C = 20.0
HEATER_DEFAULT_AUTO_POWER_WATTS = 500
# The display backlight toggle sends a uniform four-byte level plus a fixed
# trailer; the app's own literals (and captured frames) use 100 for on and
# 200 for off.
HEATER_BACKLIGHT_ON_LEVEL = 100
HEATER_BACKLIGHT_OFF_LEVEL = 200
HEATER_BACKLIGHT_TRAILER = 127

# Dosing-pump wire limits (reverse-engineered from My Chihiros 2.8.59):
# volumes ride in two bytes as 0.1 mL buckets (0..6553.5 mL) and the stirrer
# clamps run times to 999 seconds (see chihiros_xapk/DOSING_CONTROL.md).
DOSE_VOLUME_MAX_ML = 6553.5
MANUAL_DOSE_VOLUME_MIN_ML = 0.2
MANUAL_DOSE_VOLUME_MAX_ML = 999.9
STIRRER_MAX_SECONDS = 999
STIRRER_SPEED_DEFAULT = 40
STIRRER_MIN_POINT_GAP_MINUTES = 2
MINUTES_PER_DAY = 24 * 60
# The stirrer UI converts timer-point "dosage" volumes to minutes with
# round(dosage / 1000 / 0.6): the pump's 0.6 mL/min dosing-rate equivalence.
STIRRER_ML_PER_MINUTE = 0.6
# Free/timer schedule records batch into 0xA5 frames of at most 50 payload
# bytes (dataMaker.dart ``cmp #0x32`` batch size).
DOSING_SCHEDULE_MAX_PAYLOAD = 50


def validate_stirrer_point_gaps(starts: Sequence[int]) -> None:
    """Reject stir points closer than the app's two-minute cyclic gap."""
    ordered = sorted(starts)
    if len(ordered) < 2:
        return
    gaps = [second - first for first, second in zip(ordered, ordered[1:], strict=False)]
    gaps.append(ordered[0] + MINUTES_PER_DAY - ordered[-1])
    if min(gaps) < STIRRER_MIN_POINT_GAP_MINUTES:
        raise ValueError(f"Stir points must be at least {STIRRER_MIN_POINT_GAP_MINUTES} minutes apart")


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


def create_base_auth_command(msg_id: tuple[int, int]) -> bytearray:
    """Create the base LED auth/status command used at connection startup (app's getDeviceInfo())."""
    return create_command_encoding(90, 4, msg_id, [1])


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


def stirrer_dosage_for_minutes(minutes: float) -> float:
    """Convert stir run minutes to the dosage volume carried on the wire.

    The stirrer reuses the pump's timer-point encoding; the app equates
    ``dosage_ml / 0.6`` with run minutes (the pump's 0.6 mL/min dose rate).
    """
    return minutes * STIRRER_ML_PER_MINUTE


def stirrer_minutes_for_dosage(dosage_ml: float) -> float:
    """Convert a stirrer timer-point dosage back to run minutes."""
    return dosage_ml / STIRRER_ML_PER_MINUTE


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


def create_stirrer_pre_second_command(
    msg_id: tuple[int, int],
    channel: int,
    seconds: int,
    speed: int,
) -> bytearray:
    """Create the stirrer's ``stirrerPreSecond`` frame ``(0xA5, 42)``.

    Payload ``[channel, sec_hi, sec_lo, speed]``: the only wire carrier for
    the stir speed. ``seconds`` is the pre-stir time (0 to 999 s, the app's
    ``stirrer_time_max`` bound). The 0-100 speed range is an implementation
    assumption — the docs only pin the default of 40.
    """
    _validate_dosing_channel(channel)
    if not 0 <= seconds <= STIRRER_MAX_SECONDS:
        raise ValueError(f"Pre-stir seconds must be between 0 and {STIRRER_MAX_SECONDS}")
    if not 0 <= speed <= 100:
        raise ValueError("Stir speed must be between 0 and 100")
    parameters = [channel, seconds >> 8, seconds & 0xFF, speed]
    return create_command_encoding(165, 42, msg_id, parameters, avoid_reserved_byte=False)


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


def _auto_point_parameters(channel: int, minutes: int, level: int, *, sea_led_family: bool) -> list[int]:
    """Encode the auto-curve point payload for a model family.

    SeaLed devices use ``[channel, hour, minute, level]``; BleLed/NewBleLed
    devices use ``[channel, 30-min-slot, level]`` with the app's rounding rule
    (a remainder above 14 minutes advances to the next slot, up to 96 slots
    for 48-hour cross-day curves).
    """
    if sea_led_family:
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
    sea_led_family: bool,
) -> bytearray:
    """Create one auto-curve point (``0x5A, 6``) for a Commander/LED device.

    Time encoding depends on the model family (see ``models.sea_led_family``
    and docs/protocol.md): SeaLed devices use ``[channel, hour, minute,
    level]``; BleLed/NewBleLed devices use ``[channel, 30-min-slot, level]``
    with the app's rounding rule (a remainder above 14 minutes advances to the
    next slot, up to 96 slots for 48-hour cross-day curves).

    ``minutes`` is minutes since midnight (0..1439; up to
    :data:`AUTO_POINT_MAX_MINUTES` for cross-day curves), ``level`` is 0..100.
    Payload bytes are sent as-is — a level of 90 stays 0x5A (the app does not
    escape parameter bytes).
    """
    _validate_auto_point_parameters(channel, minutes, level)
    parameters = _auto_point_parameters(channel, minutes, level, sea_led_family=sea_led_family)
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


def split_heater_temperature(temperature_c: float) -> tuple[int, int]:
    """Encode a heater temperature as the wire ``[whole, hundredths]`` byte pair.

    The app builds this pair with ``CommonTool.getInt``/``getDec``, i.e. whole
    degrees plus the fraction in hundredths. Captured app frames confirm it:
    ``setHeaterProtectedTemp | 36.90 C`` goes out as ``24 5a`` (36, 90), and
    ``36.50`` as ``24 32`` (36, 50) — the same 2-digit fraction convention as
    the dosing pump's calibration volume.
    """
    if not 0 <= temperature_c <= HEATER_MAX_TEMPERATURE_C:
        raise ValueError(f"Heater temperature must be between 0 and {HEATER_MAX_TEMPERATURE_C} °C")
    return divmod(round(temperature_c * 100), 100)


def encode_heater_power_watts(power_watts: int) -> int:
    """Encode a heater power in watts as the wire byte (watts ÷ 10)."""
    if not 0 <= power_watts <= HEATER_MAX_POWER_WATTS:
        raise ValueError(f"Heater power must be between 0 and {HEATER_MAX_POWER_WATTS} watts")
    if power_watts % 10:
        raise ValueError("Heater power must be divisible by 10 watts")
    return power_watts // 10


def create_heater_set_command(
    msg_id: tuple[int, int],
    *,
    auto: bool,
    temperature_c: float,
    power_watts: int,
) -> bytearray:
    """Create the app's ``setHeaterCode`` frame ``(0x5A, 43)``.

    Payload ``[flag, temp_whole, temp_tenths, power]``; ``flag`` is 1 for the
    auto-mode defaults (``initAutoDefault``) and 0 for the manual setting
    (``initManual``, which the app sends right after ``switchToManual``).
    """
    whole, tenths = split_heater_temperature(temperature_c)
    parameters = [1 if auto else 0, whole, tenths, encode_heater_power_watts(power_watts)]
    return create_command_encoding(90, 43, msg_id, parameters, avoid_reserved_byte=False)


def create_heater_auto_mode_command(msg_id: tuple[int, int]) -> bytearray:
    """Create the heater's ``switchToAuto()`` frame ``(0x5A, 5, [3, 255, 255])``."""
    return create_command_encoding(90, 5, msg_id, [3, 255, 255], avoid_reserved_byte=False)


def create_heater_scene_command(msg_id: tuple[int, int]) -> bytearray:
    """Create the heater's ``switchToScene()`` frame ``(0x5A, 5, [18, 255, 255])``.

    Applies the stored scene/auto schedule; the app sends it 300 ms after
    ``resetLedQuick`` when a scene is edited.
    """
    return create_command_encoding(90, 5, msg_id, [18, 255, 255], avoid_reserved_byte=False)


def create_heater_auto_heating_command(msg_id: tuple[int, int], enabled: bool) -> bytearray:
    """Create the app's ``setHeaterAuto`` frame ``(0x5A, 5, [46|47, 255, 255])``.

    The boolean is passed through unchanged: auto heating on is sub-command 46
    and off is 47.
    """
    return create_command_encoding(90, 5, msg_id, [46 if enabled else 47, 255, 255], avoid_reserved_byte=False)


def create_heater_temperature_unit_command(msg_id: tuple[int, int], *, celsius: bool) -> bytearray:
    """Create the app's ``setTemType`` frame ``(0x5A, 5, [44|45, 255, 255])``.

    The boolean is passed through unchanged: Celsius is sub-command 44 and
    Fahrenheit is 45.
    """
    return create_command_encoding(90, 5, msg_id, [44 if celsius else 45, 255, 255], avoid_reserved_byte=False)


def create_heater_protector_temperature_command(msg_id: tuple[int, int], temperature_c: float) -> bytearray:
    """Create the app's ``setHeaterProtectedTemp`` frame ``(0x5A, 47)``.

    Payload is the ``[whole, tenths]`` temperature pair of the overheat
    protection limit.
    """
    whole, tenths = split_heater_temperature(temperature_c)
    return create_command_encoding(90, 47, msg_id, [whole, tenths], avoid_reserved_byte=False)


def create_heater_calibrate_command(msg_id: tuple[int, int], measured_temperature_c: float) -> bytearray:
    """Create the app's ``setHeaterCalibrate`` frame ``(0x5A, 48)``.

    Payload is the ``[whole, tenths]`` pair of the measured reference
    temperature the device should treat as current.
    """
    whole, tenths = split_heater_temperature(measured_temperature_c)
    return create_command_encoding(90, 48, msg_id, [whole, tenths], avoid_reserved_byte=False)


def create_heater_reset_work_time_command(msg_id: tuple[int, int]) -> bytearray:
    """Create the app's ``heaterResetWorkTime`` frame ``(0x5A, 5, [58, 255, 255])``.

    Sent after the user cleans the heating tube; it zeroes the runtime counter
    that drives the cleaning warning.
    """
    return create_command_encoding(90, 5, msg_id, [58, 255, 255], avoid_reserved_byte=False)


def create_heater_backlight_command(msg_id: tuple[int, int], *, enabled: bool) -> bytearray:
    """Create the app's ``deviceBacklight`` frame ``(0xA5, 56)``.

    The app's backlight toggle (``ScreenBackLightSettingWidget::change``) sends
    one uniform four-byte payload with a trailing ``127``: ``[100, 100, 100,
    100, 127]`` turns the display backlight on and ``[200, 200, 200, 200, 127]``
    turns it off. Both frames appear in captured app traffic (``64 64 64 64 7f``
    followed by ``c8 c8 c8 c8 7f``); the widget's other mode-56 writes carry the
    backlight schedule (start/end hour and weekday mask) and are not modelled
    here.
    """
    level = HEATER_BACKLIGHT_ON_LEVEL if enabled else HEATER_BACKLIGHT_OFF_LEVEL
    return create_command_encoding(165, 56, msg_id, [level] * 4 + [HEATER_BACKLIGHT_TRAILER], avoid_reserved_byte=False)
