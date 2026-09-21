"""Development fake for heaters."""

# These fake methods mirror runtime protocols; narrow surfaces intentionally omit docs.
# ruff: noqa: D102, D107

from __future__ import annotations

import asyncio

from ..vendor.chihiros_led_control.protocol.heater import (
    HEATER_DEFAULT_AUTO_POWER_WATTS,
    HEATER_DEFAULT_AUTO_TEMPERATURE_C,
    HEATER_DEFAULT_POWER_WATTS,
    HEATER_DEFAULT_PROTECTOR_TEMPERATURE_C,
    HEATER_DEFAULT_TEMPERATURE_C,
    HeaterStatusNotification,
    HeaterTemperatureNotification,
    heater_alarm_names,
)
from .base import FakeBaseDevice


class FakeHeaterDevice(FakeBaseDevice):
    """In-memory heater client with only heater-family operations."""

    def __init__(self, device_info) -> None:
        super().__init__(device_info)
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

    async def query_status(self) -> None:
        await asyncio.sleep(0)
        self._push_heater_notifications()

    def _push_heater_temperature(self) -> None:
        self.last_heater_temperature_notification = HeaterTemperatureNotification(
            setting_temperature_celsius=self._heater_setting_c,
            current_temperature_celsius=self._heater_current_c,
            raw=b"",
        )
        self._notify_callbacks(self.last_heater_temperature_notification)

    def _push_heater_status(self) -> None:
        self.last_heater_status_notification = HeaterStatusNotification(
            firmware_version=15,
            work_time_hours=self._heater_work_hours,
            alarms=self._heater_alarms,
            raw=b"",
        )
        self._notify_callbacks(self.last_heater_status_notification)

    def _push_heater_notifications(self) -> None:
        self._push_heater_temperature()
        self._push_heater_status()

    async def set_temperature(self, temperature_c: float) -> None:
        await self.set_manual_state(temperature_c, self._heater_power_watts)

    async def set_power(self, power_watts: int) -> None:
        await self.set_manual_state(self._heater_setting_c, power_watts)

    async def set_manual_state(self, temperature_c: float, power_watts: int) -> None:
        await asyncio.sleep(0)
        self._heater_setting_c = temperature_c
        self._heater_power_watts = power_watts
        self._push_heater_temperature()

    async def set_manual_mode(self) -> None:
        await asyncio.sleep(0)

    async def apply_scene(self) -> None:
        await asyncio.sleep(0)

    async def set_auto_default_temperature(self, temperature_c: float) -> None:
        await self.set_auto_defaults(temperature_c, self._heater_auto_power_watts)

    async def set_auto_default_power(self, power_watts: int) -> None:
        await self.set_auto_defaults(self._heater_auto_temperature_c, power_watts)

    async def set_auto_defaults(self, temperature_c: float, power_watts: int) -> None:
        await asyncio.sleep(0)
        self._heater_auto_temperature_c = temperature_c
        self._heater_auto_power_watts = power_watts

    def restore_setting_temperature(self, temperature_c: float) -> None:
        self._heater_setting_c = temperature_c

    def restore_manual_power(self, power_watts: int) -> None:
        self._heater_power_watts = power_watts

    def restore_auto_default_temperature(self, temperature_c: float) -> None:
        self._heater_auto_temperature_c = temperature_c

    def restore_auto_default_power(self, power_watts: int) -> None:
        self._heater_auto_power_watts = power_watts

    async def set_auto_heating(self, enabled: bool) -> None:
        await asyncio.sleep(0)
        self._heater_auto_heating = enabled

    async def set_temperature_unit(self, *, celsius: bool) -> None:
        await asyncio.sleep(0)
        self._heater_celsius = celsius

    async def set_backlight(self, enabled: bool) -> None:
        await asyncio.sleep(0)
        self._heater_backlight = enabled

    async def set_protector_temperature(self, temperature_c: float) -> None:
        await asyncio.sleep(0)
        self._heater_protector_c = temperature_c

    async def calibrate(self, measured_temperature_c: float) -> None:
        await asyncio.sleep(0)
        self._heater_current_c = measured_temperature_c
        self._push_heater_temperature()

    async def reset_work_time(self) -> None:
        await asyncio.sleep(0)
        self._heater_work_hours = 0
        self._push_heater_status()

    @property
    def setting_temperature_celsius(self) -> float:
        return self._heater_setting_c

    @property
    def current_temperature_celsius(self) -> float:
        return self._heater_current_c

    @property
    def power_watts(self) -> int:
        return self._heater_power_watts

    @property
    def protector_temperature_celsius(self) -> float:
        return self._heater_protector_c

    @property
    def auto_default_temperature_celsius(self) -> float:
        return self._heater_auto_temperature_c

    @property
    def auto_default_power_watts(self) -> int:
        return self._heater_auto_power_watts

    @property
    def auto_heating(self) -> bool:
        return self._heater_auto_heating

    @property
    def is_celsius(self) -> bool:
        return self._heater_celsius

    @property
    def backlight(self) -> bool:
        return self._heater_backlight

    @property
    def work_time_hours(self) -> int:
        return self._heater_work_hours

    @property
    def heater_alarms(self) -> tuple[str, ...]:
        return heater_alarm_names(self._heater_alarms)

    @property
    def firmware_version(self) -> int:
        notification = self.last_heater_status_notification
        return notification.firmware_version if notification else 15


__all__ = ["FakeHeaterDevice"]
