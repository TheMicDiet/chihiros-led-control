"""Heater family commands and notification codec."""

from __future__ import annotations

from types import MappingProxyType

from .frame import create_command_encoding
from .notifications import HeaterStatusNotification, HeaterTemperatureNotification

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


def create_switch_to_manual_mode_command(msg_id: tuple[int, int]) -> bytearray:
    """Create the common manual-mode frame used before heater writes."""
    return create_command_encoding(90, 5, msg_id, [11, 255, 255])


def create_query_status_command(msg_id: tuple[int, int]) -> bytearray:
    """Create the common status-query frame used by heater clients."""
    return create_command_encoding(90, 4, msg_id, [1])


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


def heater_alarm_names(alarms: int) -> tuple[str, ...]:
    """Return human-readable alarm names for an alarm bitmap."""
    return tuple(name for name, bit in HEATER_ALARM_BITS.items() if alarms & bit)


def parse_notification(data: bytes | bytearray):
    """Parse heater temperature/status notifications, or return ``None``."""
    if len(data) < 7 or data[0] != 0x5B:
        return None
    mode = data[5]
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


__all__ = [
    "HEATER_MAX_POWER_WATTS",
    "HEATER_MAX_TEMPERATURE_C",
    "HEATER_DEFAULT_TEMPERATURE_C",
    "HEATER_DEFAULT_POWER_WATTS",
    "HEATER_DEFAULT_PROTECTOR_TEMPERATURE_C",
    "HEATER_DEFAULT_AUTO_TEMPERATURE_C",
    "HEATER_DEFAULT_AUTO_POWER_WATTS",
    "HEATER_BACKLIGHT_ON_LEVEL",
    "HEATER_BACKLIGHT_OFF_LEVEL",
    "HEATER_BACKLIGHT_TRAILER",
    "split_heater_temperature",
    "encode_heater_power_watts",
    "create_switch_to_manual_mode_command",
    "create_query_status_command",
    "create_heater_auto_mode_command",
    "create_heater_scene_command",
    "create_heater_auto_heating_command",
    "create_heater_temperature_unit_command",
    "create_heater_protector_temperature_command",
    "create_heater_calibrate_command",
    "create_heater_reset_work_time_command",
    "create_heater_backlight_command",
    "HEATER_ALARM_BITS",
    "heater_alarm_names",
    "parse_notification",
    "HeaterStatusNotification",
    "HeaterTemperatureNotification",
]
