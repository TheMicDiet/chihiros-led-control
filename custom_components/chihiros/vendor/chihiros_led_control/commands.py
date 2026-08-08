"""High-level Chihiros command builders."""

from __future__ import annotations

import datetime
from collections.abc import Sequence

from .protocol import create_command_encoding, encode_timestamp

AUTO_SETTING_PARAMETER_COUNT = 14
AUTO_SETTING_METADATA_PARAMETER_COUNT = 6
DOSE_VOLUME_BUCKET_TENTHS_ML = 256


def create_base_auth_command(msg_id: tuple[int, int]) -> bytearray:
    """Create the base LED auth/status command used at connection startup.

    Verified byte-identical to the 2.8.59 vendor app's ``getDeviceInfo()``
    (``0x5A, 4, [1]`` — the app stores the constant 2, which the smi-tagged
    frame encoder unboxes to wire byte 1).
    """
    return create_command_encoding(90, 4, msg_id, [1])


def split_dose_volume_ml(ml: float) -> tuple[int, int]:
    """Encode a dosing pump volume as 25.6 mL buckets plus 0.1 mL remainder."""
    if ml < 0.2 or ml > 999.9:
        raise ValueError("Dose volume must be between 0.2 and 999.9 mL")
    tenths_ml = int(round(ml * 10))
    return divmod(tenths_ml, DOSE_VOLUME_BUCKET_TENTHS_ML)


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


def create_switch_to_auto_mode_command(msg_id: tuple[int, int]) -> bytearray:
    """Create a switch to auto mode command.

    The payload matches the vendor app's ``switchToScene()`` frame
    ``(0x5A, 5, [18, 255, 255])`` which activates the stored auto schedule.
    NOTE (app 2.8.59): the app has a *separate* ``switchToAuto()`` =
    ``(0x5A, 5, [3, 255, 255])``; this command keeps the ``[18, 255, 255]``
    form (the schedule-driven auto mode used by LED devices).
    """
    return create_command_encoding(90, 5, msg_id, [18, 255, 255])


def create_switch_to_manual_mode_command(msg_id: tuple[int, int]) -> bytearray:
    """Create a switch to manual mode command.

    The vendor app sends ``switchToManual()`` = ``(0x5A, 5, [11, 255, 255])``
    before manual slider control on LED devices.
    """
    return create_command_encoding(90, 5, msg_id, [11, 255, 255])


def create_set_fan_speed_command(msg_id: tuple[int, int], speed_percent: int) -> bytearray:
    """Create a fan speed command for fan-equipped LED devices such as the WRGB VIVID III."""
    if speed_percent < 0 or speed_percent > 100:
        raise ValueError("Fan speed must be between 0 and 100 percent")
    return create_command_encoding(90, 15, msg_id, [speed_percent])


def create_fan_auto_mode_command(msg_id: tuple[int, int]) -> bytearray:
    """Create a fan auto mode command.

    Matches the vendor app's ``LedInfo::setFanAuto()`` → ``DataMaker::autoFan()``
    frame ``(0x5A, 5, [0x11, 0xFF, 0xFF])`` used by fan-equipped LED models such
    as the WRGB VIVID III. In auto mode the device starts/stops the fan from its
    configured temperature thresholds.
    """
    return create_command_encoding(90, 5, msg_id, [0x11, 0xFF, 0xFF])


def create_vivid3_fan_start_stop_temp_command(
    msg_id: tuple[int, int],
    start_temp: int,
    stop_temp: int,
) -> bytearray:
    """Create a VIVID3 fan start/stop temperature command.

    Matches the vendor app's ``vvd3FanStartStopTemp()`` frame
    ``(0xA5, 45, [start ?? 38, stop ?? 33])``. The fan starts when the
    temperature reaches ``start_temp`` and stops at ``stop_temp`` (the app
    defaults are 38 / 33 °C, a 5 °C hysteresis). Temperatures are payload data
    so reserved-byte avoidance is disabled to send values such as 90 °C
    verbatim.
    """
    if not 0 <= start_temp <= 255 or not 0 <= stop_temp <= 255:
        raise ValueError("Fan temperatures must be between 0 and 255")
    return create_command_encoding(165, 45, msg_id, [start_temp, stop_temp], avoid_reserved_byte=False)
