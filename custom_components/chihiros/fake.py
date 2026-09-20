"""Development-only fake Chihiros devices for local Home Assistant testing."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from .dosing import normalize_pump_count
from .vendor.chihiros_led_control.commands import (
    HEATER_DEFAULT_AUTO_POWER_WATTS,
    HEATER_DEFAULT_AUTO_TEMPERATURE_C,
    HEATER_DEFAULT_POWER_WATTS,
    HEATER_DEFAULT_PROTECTOR_TEMPERATURE_C,
    HEATER_DEFAULT_TEMPERATURE_C,
)
from .vendor.chihiros_led_control.models import (
    DOSING_PUMP,
    HEATER,
    MAG_STIRRER,
    RGB_CHANNELS,
    WHITE_CHANNELS,
    WRGB_CHANNELS,
    X300_CHANNELS,
    DeviceModel,
)
from .vendor.chihiros_led_control.protocol import (
    DosingDailyNotification,
    DosingTotalsNotification,
    FanStatusNotification,
    HeaterStatusNotification,
    HeaterTemperatureNotification,
    ParsedNotification,
    RuntimeNotification,
    SchedulePoint,
    ScheduleSnapshotNotification,
    heater_alarm_names,
)

FAKE_DEVICES_ENV = "CHIHIROS_FAKE_DEVICES"
FAKE_ADDRESS_PREFIX = "FA:CE:C0"


@dataclass(frozen=True)
class FakeChihirosDeviceInfo:
    """Static fake device metadata."""

    address: str
    name: str
    model: DeviceModel


FAKE_DEVICES = (
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:01",
        name="DYNW60-fake",
        model=DeviceModel("Fake WRGB II", ("DYNW60",), RGB_CHANNELS),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:02",
        name="DYWPRO60-fake",
        model=DeviceModel("Fake WRGB II Pro", ("DYWPRO60",), WRGB_CHANNELS),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:03",
        name="DYNA2-fake",
        model=DeviceModel("Fake A II", ("DYNA2",), WHITE_CHANNELS),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:04",
        name="DYDOSE-fake",
        model=DOSING_PUMP,
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:05",
        name="DYVVD3-fake",
        model=DeviceModel(
            "Fake WRGB VIVID III",
            ("DYVVD3",),
            WRGB_CHANNELS,
            has_fan=True,
            min_fan_speed=25,
            is_vivid3=True,
        ),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:06",
        name="DYA-fake",
        model=DeviceModel("Fake A Series", ("DYA",), WHITE_CHANNELS),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:07",
        name="DYC-fake",
        model=DeviceModel("Fake New C", ("DYC",), WHITE_CHANNELS),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:08",
        name="DYARGB-fake",
        model=DeviceModel("Fake RGB+APLUS", ("DYARGB",), RGB_CHANNELS),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:09",
        name="DYREE-fake",
        model=DeviceModel("Fake RGB VIVID", ("DYREE",), RGB_CHANNELS),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:0A",
        name="DYRGBV-fake",
        model=DeviceModel("Fake RGB VIVID II", ("DYRGBV",), RGB_CHANNELS),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:0B",
        name="DYSEA-fake",
        model=DeviceModel("Fake SEA_LED", ("DYSEA",), WRGB_CHANNELS),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:0C",
        name="DYONE-fake",
        model=DeviceModel("Fake Commander X", ("DYONE",), WHITE_CHANNELS),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:0D",
        name="DYTWO-fake",
        model=DeviceModel("Fake X300", ("DYTWO",), X300_CHANNELS),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:0E",
        name="DYNLED-fake",
        model=DeviceModel(
            "Fake Commander 4",
            ("DYNLED",),
            WRGB_CHANNELS,
            sea_led_family=True,
        ),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:0F",
        name="DYMIXR-fake",
        model=MAG_STIRRER,
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:10",
        name="DYHET-fake",
        model=HEATER,
    ),
)
FAKE_DEVICES_BY_ADDRESS = {device.address: device for device in FAKE_DEVICES}

NotificationCallback = Callable[[ParsedNotification], None]


def fake_devices_enabled() -> bool:
    """Return whether fake devices are enabled for local development."""
    return os.environ.get(FAKE_DEVICES_ENV, "").lower() in {"1", "true", "yes", "on"}


def is_fake_address(address: str) -> bool:
    """Return whether an address belongs to a configured fake device."""
    return address in FAKE_DEVICES_BY_ADDRESS


def create_fake_device(address: str, pump_count: int = 4) -> FakeChihirosDevice:
    """Create a fake Chihiros device from a fake address."""
    return FakeChihirosDevice(FAKE_DEVICES_BY_ADDRESS[address], pump_count)


def iter_enabled_fake_devices(current_addresses: Iterable[str]) -> tuple[FakeChihirosDeviceInfo, ...]:
    """Return fake devices that can be shown in discovery."""
    if not fake_devices_enabled():
        return ()
    configured_addresses = set(current_addresses)
    return tuple(device for device in FAKE_DEVICES if device.address not in configured_addresses)


class FakeChihirosDevice:
    """Small in-memory Chihiros device replacement for HA UI testing."""

    def __init__(self, device_info: FakeChihirosDeviceInfo, pump_count: int = 4) -> None:
        """Initialize the fake device."""
        self._device_info = device_info
        self.pump_count = normalize_pump_count(pump_count)
        self.model = device_info.model
        self._callbacks: set[NotificationCallback] = set()
        self._brightness = {color: 0 for color in self.model.color_channels}
        self._dosed_ml = [0.0] * self.pump_count
        self._auto_mode = False
        self._auto_curve_points: list[tuple[int, int, int]] = []
        self._fan_speed = 0
        self._fan_auto = False
        self._fan_start_temp = 38
        self._fan_stop_temp = 33
        self._temp_protect = False
        self._bluetooth_led = False
        # Heater state (device-reported values plus the write-only settings).
        self._heater_setting_c = HEATER_DEFAULT_TEMPERATURE_C
        self._heater_current_c = HEATER_DEFAULT_TEMPERATURE_C - 1.0
        self._heater_power_watts = HEATER_DEFAULT_POWER_WATTS
        self._heater_protector_c = HEATER_DEFAULT_PROTECTOR_TEMPERATURE_C
        self._heater_auto_temperature_c = HEATER_DEFAULT_AUTO_TEMPERATURE_C
        self._heater_auto_power_watts = HEATER_DEFAULT_AUTO_POWER_WATTS
        self._heater_auto_heating = False
        self._heater_celsius = True
        self._heater_backlight = True
        self._heater_work_hours = 120
        self._heater_alarms = 0
        self.last_heater_temperature_notification: HeaterTemperatureNotification | None = None
        self.last_heater_status_notification: HeaterStatusNotification | None = None
        self.last_runtime_notification: RuntimeNotification | None = None
        self.last_fan_status_notification: FanStatusNotification | None = None
        self.last_schedule_snapshot_notification: ScheduleSnapshotNotification | None = None
        self.last_dosing_totals_notification: DosingTotalsNotification | None = None
        self.last_dosing_daily_notification: DosingDailyNotification | None = None
        # Magnetic-stirrer recording state (channel index keyed).
        self.stir_running: dict[int, bool] = {}
        self.stir_speeds: dict[int, int] = {}
        self.stir_pre_seconds: dict[int, int] = {}
        self.stir_schedules: list[tuple[int, tuple[tuple[int, int, float], ...], int, bool]] = []
        # Pump programming writes (set_channel_active/apply_dosing_settings/set_schedule/set_dose_delay).
        self.dosing_programming_calls: list[dict[str, object]] = []
        # Calibration wizard writes (calibrate_channel).
        self.calibration_calls: list[dict[str, object]] = []
        # Verbatim broadcast frames received via send_frame (master/slave replay).
        self.broadcast_frames: list[bytes] = []

    @property
    def address(self) -> str:
        """Return the fake BLE address."""
        return self._device_info.address

    @property
    def name(self) -> str:
        """Return the fake device name."""
        return self._device_info.name

    @property
    def model_name(self) -> str:
        """Return the fake model name."""
        return self.model.name

    @property
    def colors(self) -> dict[str, int]:
        """Return supported fake color channels."""
        return dict(self.model.color_channels)

    def add_notification_callback(self, callback: NotificationCallback) -> Callable[[], None]:
        """Register a callback for fake parsed notifications."""
        self._callbacks.add(callback)

        def remove_callback() -> None:
            self._callbacks.discard(callback)

        return remove_callback

    async def query_status(self) -> None:
        """Publish fake runtime and schedule notifications."""
        await asyncio.sleep(0)
        if self.model.is_heater:
            self._push_heater_notifications()
            return
        self.last_runtime_notification = RuntimeNotification(
            firmware_version=23,
            runtime_minutes=511,
            raw=bytes.fromhex("5b 17 0a 00 01 0a 01 ff ff ff ff 0c 36 2d"),
        )
        self.last_schedule_snapshot_notification = ScheduleSnapshotNotification(
            firmware_version=23,
            points=(
                self._schedule_point(8, 0, 15),
                self._schedule_point(12, 0, 70),
                self._schedule_point(20, 30, 0),
            ),
        )
        self._notify_callbacks(self.last_runtime_notification)
        self._notify_callbacks(self.last_schedule_snapshot_notification)
        if self.model.name == DOSING_PUMP.name:
            self.last_dosing_totals_notification = self._dosing_totals_notification()
            self.last_dosing_daily_notification = self._dosing_daily_notification()
            self._notify_callbacks(self.last_dosing_totals_notification)
            self._notify_callbacks(self.last_dosing_daily_notification)

    def _apply_scalar_brightness(self, brightness: int) -> None:
        """Apply one brightness level to every channel."""
        for color in self._brightness:
            self._brightness[color] = brightness

    def _apply_mapping_brightness(self, brightness: Mapping[str | int, int]) -> None:
        """Apply brightness levels keyed by color name or id."""
        for color, level in brightness.items():
            if isinstance(color, str) and color in self._brightness:
                self._brightness[color] = level

    async def set_brightness(self, brightness: int | Sequence[int] | Mapping[str | int, int]) -> None:
        """Set fake brightness state."""
        await asyncio.sleep(0)
        if isinstance(brightness, int):
            self._apply_scalar_brightness(brightness)
            return
        if isinstance(brightness, Mapping):
            self._apply_mapping_brightness(brightness)
            return
        for color, level in zip(self._brightness, brightness, strict=False):
            self._brightness[color] = level

    async def turn_on(self) -> None:
        """Turn on all fake channels."""
        await self.set_brightness(100)

    async def turn_off(self) -> None:
        """Turn off all fake channels."""
        await self.set_brightness(0)

    async def enable_auto_mode(self, timestamp: datetime | None = None) -> None:
        """Enable fake auto mode."""
        del timestamp
        self._auto_mode = True
        await self.query_status()

    async def set_manual_mode(self) -> None:
        """Enable fake manual mode."""
        self._auto_mode = False

    async def set_fan_speed(self, speed_percent: int) -> None:
        """Set fake fan speed and publish a fake fan status notification."""
        await asyncio.sleep(0)
        if not self.model.has_fan:
            raise ValueError(f"Model does not support fan control: {self.model.name}")
        if speed_percent < 0 or speed_percent > 100:
            raise ValueError("Fan speed must be between 0 and 100 percent")
        if 0 < speed_percent < self.model.min_fan_speed:
            speed_percent = self.model.min_fan_speed
        self._fan_speed = speed_percent
        self._fan_auto = False
        self.last_fan_status_notification = FanStatusNotification(
            firmware_version=27,
            fan_rpm=speed_percent * 20,
            temperature_celsius=25,
        )
        self._notify_callbacks(self.last_fan_status_notification)

    async def set_fan_auto(self) -> None:
        """Switch the fake fan to temperature-controlled auto mode."""
        await asyncio.sleep(0)
        if not self.model.has_fan:
            raise ValueError(f"Model does not support fan control: {self.model.name}")
        self._fan_auto = True

    async def set_fan_start_stop_temp(self, start_temp: int, stop_temp: int) -> None:
        """Store the fake fan auto-mode start/stop temperatures."""
        await asyncio.sleep(0)
        if not self.model.has_fan:
            raise ValueError(f"Model does not support fan control: {self.model.name}")
        self._fan_start_temp = start_temp
        self._fan_stop_temp = stop_temp

    async def set_temp_protect(self, enabled: bool) -> None:
        """Track the fake VIVID3 temperature-protection state optimistically."""
        await asyncio.sleep(0)
        if not self.model.is_vivid3:
            raise ValueError(f"Model does not support temperature protection: {self.model.name}")
        self._temp_protect = enabled

    async def set_bluetooth_led(self, enabled: bool) -> None:
        """Track the fake VIVID3 indicator-LED state optimistically."""
        await asyncio.sleep(0)
        if not self.model.is_vivid3:
            raise ValueError(f"Model does not support the indicator LED switch: {self.model.name}")
        self._bluetooth_led = enabled

    @property
    def fan_auto(self) -> bool:
        """Return whether the fake fan is in auto mode."""
        return self._fan_auto

    @property
    def fan_start_temp(self) -> int:
        """Return the fake fan start temperature."""
        return self._fan_start_temp

    @property
    def fan_stop_temp(self) -> int:
        """Return the fake fan stop temperature."""
        return self._fan_stop_temp

    @property
    def temp_protect(self) -> bool:
        """Return the fake VIVID3 temperature-protection state."""
        return self._temp_protect

    @property
    def bluetooth_led(self) -> bool:
        """Return the fake VIVID3 indicator-LED state."""
        return self._bluetooth_led

    async def add_setting(
        self,
        sunrise: datetime,
        sunset: datetime,
        max_brightness: int | Sequence[int] | Mapping[str | int, int] = 100,
        ramp_up_in_minutes: int = 0,
        weekdays: list[object] | None = None,
    ) -> None:
        """Accept fake schedule writes."""
        del sunrise, sunset, max_brightness, ramp_up_in_minutes, weekdays
        await self.query_status()

    async def remove_setting(
        self,
        sunrise: datetime,
        sunset: datetime,
        ramp_up_in_minutes: int = 0,
        weekdays: list[object] | None = None,
    ) -> None:
        """Accept fake schedule deletes."""
        del sunrise, sunset, ramp_up_in_minutes, weekdays
        await self.query_status()

    async def reset_settings(self) -> None:
        """Accept fake schedule resets."""
        self._auto_curve_points.clear()
        await self.query_status()

    async def set_auto_point(self, channel: int, minutes: int, level: int) -> None:
        """Record a fake auto-curve point."""
        await asyncio.sleep(0)
        self._auto_curve_points.append((channel, minutes, level))

    async def set_auto_curve(self, points: Sequence[tuple[int, int, int]]) -> None:
        """Record a fake auto-curve batch."""
        await asyncio.sleep(0)
        self._auto_curve_points.extend(points)

    async def dose_ml(self, pump_idx: int, volume_ml: float) -> bytes:
        """Record a fake manual dose for local dosing pump testing."""
        await asyncio.sleep(0)
        self._dosed_ml[pump_idx] = round(self._dosed_ml[pump_idx] + volume_ml, 1)
        self.last_dosing_totals_notification = self._dosing_totals_notification()
        self.last_dosing_daily_notification = self._dosing_daily_notification()
        self._notify_callbacks(self.last_dosing_totals_notification)
        self._notify_callbacks(self.last_dosing_daily_notification)
        return b""

    async def reset_channel(self, channel: int) -> bytes:
        """Record a fake channel reset."""
        await asyncio.sleep(0)
        self.dosing_programming_calls.append({"kind": "reset", "channel": channel})
        return b""

    async def calibrate_channel(
        self,
        channel: int,
        *,
        seconds: int | None = None,
        volume_ml: float | None = None,
    ) -> bytes:
        """Record a fake calibration test dose or measured volume."""
        await asyncio.sleep(0)
        self.calibration_calls.append({"channel": channel, "seconds": seconds, "volume_ml": volume_ml})
        return b""

    async def send_frame(self, frame: bytes | bytearray) -> None:
        """Record a verbatim broadcast frame (master/slave replay)."""
        await asyncio.sleep(0)
        self.broadcast_frames.append(bytes(frame))

    async def stir(self, channel: int, on: bool, *, seconds: int | None = None) -> None:
        """Record a fake manual stir start/stop."""
        await asyncio.sleep(0)
        del seconds
        self.stir_running[channel] = on

    async def set_pre_second(
        self,
        channel: int,
        seconds: int,
        speed: int = 40,
        *,
        restart: bool = False,
    ) -> None:
        """Record fake stir speed and pre-stir values."""
        await asyncio.sleep(0)
        del restart
        self.stir_speeds[channel] = speed
        self.stir_pre_seconds[channel] = seconds

    async def set_stir_schedule(
        self,
        channel: int,
        points: Sequence[object],
        *,
        frequency: int = 127,
        active: bool = True,
        is_first_setting: bool = True,
    ) -> None:
        """Record a fake timer-mode schedule write."""
        await asyncio.sleep(0)
        del is_first_setting
        records = tuple(
            (point.start_hour, point.start_minute, point.volume_ml)  # type: ignore[attr-defined]
            for point in points
        )
        self.stir_schedules.append((channel, records, frequency, active))

    async def set_channel_active(self, channel: int, *, active: bool = True, compensate: bool = False) -> None:
        """Record a fake channel active/compensation write."""
        await asyncio.sleep(0)
        self.dosing_programming_calls.append(
            {"kind": "active", "channel": channel, "active": active, "compensate": compensate}
        )

    async def apply_dosing_settings(
        self,
        channel: int,
        dose_per_day_ml: float | None,
        frequency: int = 127,
        *,
        is_first_setting: bool = True,
    ) -> None:
        """Record a fake dosingSet write."""
        await asyncio.sleep(0)
        self.dosing_programming_calls.append(
            {"kind": "daily", "channel": channel, "ml": dose_per_day_ml, "frequency": frequency}
        )

    async def set_schedule(self, channel: int, mode: object, points: Sequence[object]) -> None:
        """Record a fake dosingWorkNew write."""
        await asyncio.sleep(0)
        self.dosing_programming_calls.append(
            {"kind": "schedule", "channel": channel, "mode": getattr(mode, "name", str(mode)), "points": tuple(points)}
        )

    async def set_dose_delay(self, enabled: bool) -> None:
        """Record a fake dose-delay write."""
        await asyncio.sleep(0)
        self.dosing_programming_calls.append({"kind": "delay", "enabled": enabled})

    def _push_heater_temperature(self) -> None:
        """Publish a fake heater temperature frame."""
        self.last_heater_temperature_notification = HeaterTemperatureNotification(
            setting_temperature_celsius=self._heater_setting_c,
            current_temperature_celsius=self._heater_current_c,
            raw=b"",
        )
        self._notify_callbacks(self.last_heater_temperature_notification)

    def _push_heater_status(self) -> None:
        """Publish a fake heater status frame."""
        self.last_heater_status_notification = HeaterStatusNotification(
            firmware_version=15,
            work_time_hours=self._heater_work_hours,
            alarms=self._heater_alarms,
            raw=b"",
        )
        self._notify_callbacks(self.last_heater_status_notification)

    def _push_heater_notifications(self) -> None:
        """Publish both fake heater notification frames."""
        self._push_heater_temperature()
        self._push_heater_status()

    async def set_temperature(self, temperature_c: float) -> None:
        """Store the fake target temperature and echo it back as a notification."""
        await self.set_manual_state(temperature_c, self._heater_power_watts)

    async def set_power(self, power_watts: int) -> None:
        """Store the fake manual power (the device does not report it back)."""
        await self.set_manual_state(self._heater_setting_c, power_watts)

    async def set_manual_state(self, temperature_c: float, power_watts: int) -> None:
        """Store both fake manual values and echo the reported temperature."""
        await asyncio.sleep(0)
        self._heater_setting_c = temperature_c
        self._heater_power_watts = power_watts
        self._push_heater_temperature()

    async def apply_scene(self) -> None:
        """Mark the fake device as running its stored auto schedule."""
        await asyncio.sleep(0)
        self._auto_mode = True

    async def set_auto_defaults(self, temperature_c: float, power_watts: int) -> None:
        """Store the fake auto-mode defaults (the device does not report them)."""
        await asyncio.sleep(0)
        self._heater_auto_temperature_c = temperature_c
        self._heater_auto_power_watts = power_watts

    async def set_auto_default_temperature(self, temperature_c: float) -> None:
        """Store the fake auto default temperature, resending the tracked power."""
        await self.set_auto_defaults(temperature_c, self._heater_auto_power_watts)

    async def set_auto_default_power(self, power_watts: int) -> None:
        """Store the fake auto default power, resending the tracked temperature."""
        await self.set_auto_defaults(self._heater_auto_temperature_c, power_watts)

    def restore_setting_temperature(self, temperature_c: float) -> None:
        """Restore fake manual target temperature without publishing a notification."""
        self._heater_setting_c = temperature_c

    def restore_manual_power(self, power_watts: int) -> None:
        """Restore fake manual power without publishing a notification."""
        self._heater_power_watts = power_watts

    def restore_auto_default_temperature(self, temperature_c: float) -> None:
        """Restore the fake auto temperature without publishing a notification."""
        self._heater_auto_temperature_c = temperature_c

    def restore_auto_default_power(self, power_watts: int) -> None:
        """Restore fake auto power without publishing a notification."""
        self._heater_auto_power_watts = power_watts

    async def set_auto_heating(self, enabled: bool) -> None:
        """Track the fake auto-heating state."""
        await asyncio.sleep(0)
        self._heater_auto_heating = enabled

    async def set_temperature_unit(self, *, celsius: bool) -> None:
        """Track the fake display unit."""
        await asyncio.sleep(0)
        self._heater_celsius = celsius

    async def set_backlight(self, enabled: bool) -> None:
        """Track the fake display-backlight state."""
        await asyncio.sleep(0)
        self._heater_backlight = enabled

    async def set_protector_temperature(self, temperature_c: float) -> None:
        """Track the fake overheat protection temperature."""
        await asyncio.sleep(0)
        self._heater_protector_c = temperature_c

    async def calibrate(self, measured_temperature_c: float) -> None:
        """Adopt the fake measured temperature as the current reading."""
        await asyncio.sleep(0)
        self._heater_current_c = measured_temperature_c
        self._push_heater_temperature()

    async def reset_work_time(self) -> None:
        """Zero the fake runtime counter and push the new status."""
        await asyncio.sleep(0)
        self._heater_work_hours = 0
        self._push_heater_status()

    @property
    def setting_temperature_celsius(self) -> float:
        """Return the fake target temperature."""
        return self._heater_setting_c

    @property
    def current_temperature_celsius(self) -> float:
        """Return the fake measured temperature."""
        return self._heater_current_c

    @property
    def power_watts(self) -> int:
        """Return the fake manual power."""
        return self._heater_power_watts

    @property
    def protector_temperature_celsius(self) -> float:
        """Return the fake overheat protection temperature."""
        return self._heater_protector_c

    @property
    def auto_default_temperature_celsius(self) -> float:
        """Return the fake auto-mode default temperature."""
        return self._heater_auto_temperature_c

    @property
    def auto_default_power_watts(self) -> int:
        """Return the fake auto-mode default power."""
        return self._heater_auto_power_watts

    @property
    def auto_heating(self) -> bool:
        """Return the fake auto-heating state."""
        return self._heater_auto_heating

    @property
    def is_celsius(self) -> bool:
        """Return whether the fake device displays Celsius."""
        return self._heater_celsius

    @property
    def backlight(self) -> bool:
        """Return the fake display-backlight state."""
        return self._heater_backlight

    @property
    def work_time_hours(self) -> int:
        """Return the fake heating runtime."""
        return self._heater_work_hours

    @property
    def heater_alarms(self) -> tuple[str, ...]:
        """Return the fake alarm names."""
        return heater_alarm_names(self._heater_alarms)

    @property
    def firmware_version(self) -> int:
        """Return the fake heater firmware version."""
        notification = self.last_heater_status_notification
        return notification.firmware_version if notification else 15

    async def program_channel(
        self,
        channel: int,
        *,
        active: bool,
        compensate: bool = False,
        dose_per_day_ml: float | None = None,
        frequency: int = 127,
        is_first_setting: bool = True,
        mode: object = None,
        points: Sequence[object] = (),
    ) -> None:
        """Record a fake single-transaction channel programming."""
        await asyncio.sleep(0)
        self.dosing_programming_calls.append(
            {
                "kind": "program",
                "channel": channel,
                "active": active,
                "compensate": compensate,
                "ml": dose_per_day_ml,
                "frequency": frequency,
                "first_setting": is_first_setting,
                "mode": getattr(mode, "name", None),
                "points": tuple(points),
            }
        )

    def _dosing_totals_notification(self) -> DosingTotalsNotification:
        """Return the fake pump's lifetime totals as a device notification."""
        return DosingTotalsNotification(
            tuple(round(volume * 1000) for volume in self._dosed_ml),
            raw=b"",
        )

    def _dosing_daily_notification(self) -> DosingDailyNotification:
        """Return the fake pump's dosed-today totals as a device notification."""
        return DosingDailyNotification(
            tuple(round(volume * 1000) for volume in self._dosed_ml),
            raw=b"",
        )

    async def disconnect(self) -> None:
        """Disconnect the fake device."""
        await asyncio.sleep(0)

    def _schedule_point(self, hour: int, minute: int, level: int) -> SchedulePoint:
        """Create a schedule point for all fake channels."""
        return SchedulePoint(
            hour=hour,
            minute=minute,
            levels={color: level for color in self.model.color_channels},
        )

    def _notify_callbacks(self, notification: ParsedNotification) -> None:
        """Notify fake subscribers."""
        for callback in tuple(self._callbacks):
            callback(notification)
