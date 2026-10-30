"""Heater family commands and notification codec."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from .frame import create_command_encoding
from .notifications import HeaterPowerNotification, HeaterStatusNotification, HeaterTemperatureNotification

HEATER_MAX_POWER_WATTS = 2000
HEATER_MAX_TEMPERATURE_C = 100.0
# Vendor-app model defaults (``ChihirosHeater::init``): 25.0 °C setting
# temperature, 200 W manual power and a 37.0 °C overheat protector. The
# disassembly stores Dart smis, i.e. half the raw immediates quoted in
# ``HEATER_CONTROL.md`` §2.
HEATER_DEFAULT_TEMPERATURE_C = 25.0
HEATER_DEFAULT_POWER_WATTS = 200
HEATER_DEFAULT_PROTECTOR_TEMPERATURE_C = 37.0
# APK-verified always-on/off sentinels; scheduled writes use clock fields instead.
HEATER_BACKLIGHT_ON_LEVEL = 200
HEATER_BACKLIGHT_OFF_LEVEL = 100
HEATER_BACKLIGHT_TRAILER = 127

HEATER_ALARM_BITS = MappingProxyType(
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


@dataclass(frozen=True, slots=True)
class HeaterBacklightSchedule:
    """Write-only backlight schedule with Monday bit 6 through Sunday bit 0."""

    start_hour: int
    start_minute: int
    end_hour: int
    end_minute: int
    weekdays: int = 127

    def __post_init__(self) -> None:
        """Require clock fields and a seven-bit weekday mask."""
        for name, value, maximum in (
            ("start_hour", self.start_hour, 23),
            ("start_minute", self.start_minute, 59),
            ("end_hour", self.end_hour, 23),
            ("end_minute", self.end_minute, 59),
            ("weekdays", self.weekdays, 127),
        ):
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError(f"Backlight {name} must be an integer between 0 and {maximum}")


def heater_alarm_names(alarms: int) -> tuple[str, ...]:
    """Return human-readable alarm names for a heater alarm bitmap."""
    return tuple(name for name, bit in HEATER_ALARM_BITS.items() if alarms & bit)


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
    manual: bool,
    temperature_c: float,
    power_watts: int,
) -> bytearray:
    """Create the app's ``setHeaterCode`` frame ``(0x5A, 43)``.

    Payload ``[flag, temp_whole, temp_hundredths, power]`` writes the same
    target and power in either mode. Flag 0 also selects manual mode; flag 1
    preserves the active mode.
    """
    whole, hundredths = split_heater_temperature(temperature_c)
    parameters = [0 if manual else 1, whole, hundredths, encode_heater_power_watts(power_watts)]
    return create_command_encoding(90, 43, msg_id, parameters)


def create_switch_to_manual_mode_command(msg_id: tuple[int, int]) -> bytearray:
    """Create the common manual-mode frame used before heater writes."""
    return create_command_encoding(90, 5, msg_id, [11, 255, 255])


def create_query_status_command(msg_id: tuple[int, int]) -> bytearray:
    """Create the common status-query frame used by heater clients."""
    return create_command_encoding(90, 4, msg_id, [1])


def create_heater_auto_mode_command(msg_id: tuple[int, int]) -> bytearray:
    """Select the heater's automatic scene mode without changing stored scenes."""
    return create_command_encoding(90, 5, msg_id, [18, 255, 255])


def create_heater_automatic_power_command(msg_id: tuple[int, int], enabled: bool) -> bytearray:
    """Enable or disable automatic power escalation, including in manual mode.

    Sub-command 46 enables escalation and 47 disables it; neither turns the
    heating element on or off.
    """
    return create_command_encoding(90, 5, msg_id, [46 if enabled else 47, 255, 255])


def create_heater_temperature_unit_command(msg_id: tuple[int, int], *, celsius: bool) -> bytearray:
    """Create the app's ``setTemType`` frame ``(0x5A, 5, [44|45, 255, 255])``.

    The boolean is passed through unchanged: Celsius is sub-command 44 and
    Fahrenheit is 45.
    """
    return create_command_encoding(90, 5, msg_id, [44 if celsius else 45, 255, 255])


def create_heater_protector_temperature_command(msg_id: tuple[int, int], temperature_c: float) -> bytearray:
    """Create the app's ``setHeaterProtectedTemp`` frame ``(0x5A, 47)``.

    Payload is the ``[whole, tenths]`` temperature pair of the overheat
    protection limit.
    """
    whole, tenths = split_heater_temperature(temperature_c)
    return create_command_encoding(90, 47, msg_id, [whole, tenths])


def create_heater_calibrate_command(msg_id: tuple[int, int], measured_temperature_c: float) -> bytearray:
    """Create the app's ``setHeaterCalibrate`` frame ``(0x5A, 48)``.

    Payload is the ``[whole, tenths]`` pair of the measured reference
    temperature the device should treat as current.
    """
    whole, tenths = split_heater_temperature(measured_temperature_c)
    return create_command_encoding(90, 48, msg_id, [whole, tenths])


def create_heater_reset_work_time_command(msg_id: tuple[int, int]) -> bytearray:
    """Create the app's ``heaterResetWorkTime`` frame ``(0x5A, 5, [58, 255, 255])``.

    Sent after the user cleans the heating tube; it zeroes the runtime counter
    that drives the cleaning warning.
    """
    return create_command_encoding(90, 5, msg_id, [58, 255, 255])


def create_heater_backlight_command(msg_id: tuple[int, int], *, enabled: bool) -> bytearray:
    """Create the app's ``deviceBacklight`` frame ``(0xA5, 56)``.

    Enabled/always on sends ``[200, 200, 200, 200, 127]``; disabled/always off
    sends ``[100, 100, 100, 100, 127]``. Use the separate schedule encoder for
    clock-based operation.
    """
    level = HEATER_BACKLIGHT_ON_LEVEL if enabled else HEATER_BACKLIGHT_OFF_LEVEL
    return create_command_encoding(165, 56, msg_id, [level] * 4 + [HEATER_BACKLIGHT_TRAILER])


def create_heater_backlight_schedule_command(msg_id: tuple[int, int], schedule: HeaterBacklightSchedule) -> bytearray:
    """Create ``A5/38 [start_hour, start_minute, end_hour, end_minute, weekmask]``."""
    return create_command_encoding(
        165,
        56,
        msg_id,
        [schedule.start_hour, schedule.start_minute, schedule.end_hour, schedule.end_minute, schedule.weekdays],
    )


def parse_notification(data: bytes | bytearray):
    """Parse exact-length heater measurement, status and configured-power frames."""
    if len(data) < 7 or data[0] != 0x5B:
        return None
    mode = data[5]
    if mode == 0x25 and len(data) == 13:
        return HeaterTemperatureNotification(
            measured_power_watts=(data[6] << 8) | data[7],
            current_temperature_celsius=((data[10] << 8) | data[11]) / 10,
            raw=bytes(data),
        )
    if mode == 0x0A and len(data) == 16:
        return HeaterStatusNotification(
            work_time_hours=(data[7] << 8) | data[8],
            alarms=data[14],
            raw=bytes(data),
        )
    if mode == 0x0B and len(data) == 18:
        return HeaterPowerNotification(
            power_watts=data[12] * 10,
            heating_time_seconds=int.from_bytes(data[13:17], "big"),
            raw=bytes(data),
        )
    return None


__all__ = [
    "HEATER_MAX_POWER_WATTS",
    "HEATER_MAX_TEMPERATURE_C",
    "HEATER_DEFAULT_TEMPERATURE_C",
    "HEATER_DEFAULT_POWER_WATTS",
    "HEATER_DEFAULT_PROTECTOR_TEMPERATURE_C",
    "HEATER_BACKLIGHT_ON_LEVEL",
    "HEATER_BACKLIGHT_OFF_LEVEL",
    "HEATER_BACKLIGHT_TRAILER",
    "HeaterBacklightSchedule",
    "HEATER_ALARM_BITS",
    "split_heater_temperature",
    "encode_heater_power_watts",
    "create_switch_to_manual_mode_command",
    "create_query_status_command",
    "create_heater_auto_mode_command",
    "create_heater_automatic_power_command",
    "create_heater_temperature_unit_command",
    "create_heater_protector_temperature_command",
    "create_heater_calibrate_command",
    "create_heater_reset_work_time_command",
    "create_heater_backlight_command",
    "create_heater_backlight_schedule_command",
    "heater_alarm_names",
    "parse_notification",
]
