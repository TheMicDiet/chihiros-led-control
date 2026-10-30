"""Heater family driver."""

from __future__ import annotations

import asyncio

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from ..models import DeviceModel
from ..protocol import heater as commands
from ..protocol.notifications import (
    HeaterPowerNotification,
    HeaterStatusNotification,
    HeaterTemperatureNotification,
    ParsedNotification,
)
from ..registry import HEATER
from ..transport import ChihirosTransport
from .base import COMMAND_NOTIFICATION_WAIT, STATUS_NOTIFICATION_WAIT, BaseChihirosDevice


class ChihirosHeater(BaseChihirosDevice):
    """Concrete BLE client for a Chihiros heater."""

    def __init__(
        self,
        ble_device: BLEDevice,
        model: DeviceModel = HEATER,
        advertisement_data: AdvertisementData | None = None,
        *,
        transport: ChihirosTransport | None = None,
    ) -> None:
        """Create a heater client."""
        super().__init__(ble_device, model, advertisement_data, transport=transport)
        self._state_lock = asyncio.Lock()
        self._setting_temperature = commands.HEATER_DEFAULT_TEMPERATURE_C
        self._power_watts = commands.HEATER_DEFAULT_POWER_WATTS
        self._power_notification_sequence = 0
        self._protector_temperature = commands.HEATER_DEFAULT_PROTECTOR_TEMPERATURE_C
        self._automatic_power = False
        self._celsius = True
        self._backlight: bool | None = True
        self._backlight_schedule: commands.HeaterBacklightSchedule | None = None
        self.last_heater_temperature_notification: HeaterTemperatureNotification | None = None
        self.last_heater_power_notification: HeaterPowerNotification | None = None
        self.last_heater_status_notification: HeaterStatusNotification | None = None

    def _parse_notification(self, data: bytes | bytearray) -> ParsedNotification | None:
        """Parse heater measurement, configured-power and status notifications."""
        return commands.parse_notification(data)

    def _record_notification(self, parsed: ParsedNotification) -> None:
        """Record measurements without overwriting the write-only target temperature."""
        super()._record_notification(parsed)
        if isinstance(parsed, HeaterTemperatureNotification):
            self.last_heater_temperature_notification = parsed
        elif isinstance(parsed, HeaterPowerNotification):
            self.last_heater_power_notification = parsed
            self._power_watts = parsed.power_watts
            self._power_notification_sequence += 1
        elif isinstance(parsed, HeaterStatusNotification):
            self.last_heater_status_notification = parsed

    @property
    def setting_temperature_celsius(self) -> float:
        """Return the last known target temperature."""
        return self._setting_temperature

    @property
    def current_temperature_celsius(self) -> float | None:
        """Return the measured temperature of the last temperature frame, if any."""
        notification = self.last_heater_temperature_notification
        return notification.current_temperature_celsius if notification else None

    @property
    def measured_power_watts(self) -> int | None:
        """Return measured power from the last periodic measurement, if any."""
        notification = self.last_heater_temperature_notification
        return notification.measured_power_watts if notification else None

    @property
    def heating_time_seconds(self) -> int | None:
        """Return lifetime heating time from the last configured-power frame."""
        notification = self.last_heater_power_notification
        return notification.heating_time_seconds if notification else None

    @property
    def power_watts(self) -> int:
        """Return configured power, updated by writes and device readback."""
        return self._power_watts

    @property
    def protector_temperature_celsius(self) -> float:
        """Return the tracked overheat protection temperature."""
        return self._protector_temperature

    @property
    def automatic_power(self) -> bool:
        """Return whether automatic power escalation is enabled."""
        return self._automatic_power

    @property
    def is_celsius(self) -> bool:
        """Return whether the device displays Celsius (as opposed to Fahrenheit)."""
        return self._celsius

    @property
    def backlight(self) -> bool | None:
        """Return always-on/off state, or ``None`` when a schedule controls it."""
        return self._backlight

    @property
    def backlight_schedule(self) -> commands.HeaterBacklightSchedule | None:
        """Return the tracked schedule, or ``None`` for always-on/off operation."""
        return self._backlight_schedule

    @property
    def work_time_hours(self) -> int | None:
        """Return the heating runtime since the last cleaning reset, if reported."""
        notification = self.last_heater_status_notification
        return notification.work_time_hours if notification else None

    @property
    def heater_alarms(self) -> tuple[str, ...]:
        """Return the names of the alarms in the last status frame."""
        notification = self.last_heater_status_notification
        return commands.heater_alarm_names(notification.alarms) if notification else ()

    async def set_temperature(self, temperature_c: float) -> None:
        """Switch to manual mode and set the target temperature.

        Mirrors the app's ``setTemperature`` → ``initManual`` sequence: one
        paced batch with ``switchToManual`` followed by the state frame
        carrying refreshed configured power.
        """
        async with self._state_lock:
            commands.split_heater_temperature(temperature_c)
            await self.query_status()
            await self._set_manual_state_locked(temperature_c, self._power_watts)

    async def set_power(self, power_watts: int) -> None:
        """Switch to manual mode and set the power in watts."""
        async with self._state_lock:
            commands.encode_heater_power_watts(power_watts)
            await self.query_status()
            await self._set_manual_state_locked(self._setting_temperature, power_watts)

    async def set_state(self, temperature_c: float, power_watts: int) -> None:
        """Set the shared target and power while preserving the active mode."""
        async with self._state_lock:
            commands.split_heater_temperature(temperature_c)
            commands.encode_heater_power_watts(power_watts)
            await self.query_status()
            cmd = commands.create_heater_set_command(
                self.get_next_msg_id(),
                manual=False,
                temperature_c=temperature_c,
                power_watts=power_watts,
            )
            power_sequence = self._power_notification_sequence
            await self._send_command([bytes(cmd)], 3, COMMAND_NOTIFICATION_WAIT)
            self._setting_temperature = temperature_c
            if self._power_notification_sequence == power_sequence:
                self._power_watts = power_watts

    def restore_setting_temperature(self, temperature_c: float) -> None:
        """Restore the tracked target temperature without writing to the device."""
        commands.split_heater_temperature(temperature_c)
        self._setting_temperature = temperature_c

    def restore_power(self, power_watts: int) -> None:
        """Restore tracked configured power without writing to the device."""
        commands.encode_heater_power_watts(power_watts)
        self._power_watts = power_watts

    async def set_auto_mode(self) -> None:
        """Select the stored auto schedule without changing setpoints or deleting scenes."""
        cmd = commands.create_heater_auto_mode_command(self.get_next_msg_id())
        await self._send_command(cmd, 3)

    async def set_automatic_power(self, enabled: bool) -> None:
        """Enable or disable automatic power escalation without switching mode."""
        async with self._state_lock:
            cmd = commands.create_heater_automatic_power_command(self.get_next_msg_id(), enabled)
            await self._send_command(cmd, 3)
            self._automatic_power = enabled

    async def set_temperature_unit(self, *, celsius: bool) -> None:
        """Set the device's display unit (app's ``switchTemperatureType``)."""
        async with self._state_lock:
            cmd = commands.create_heater_temperature_unit_command(self.get_next_msg_id(), celsius=celsius)
            await self._send_command(cmd, 3)
            self._celsius = celsius

    async def set_protector_temperature(self, temperature_c: float) -> None:
        """Set the overheat protection temperature (app's ``setProtectorTemperature``)."""
        async with self._state_lock:
            cmd = commands.create_heater_protector_temperature_command(self.get_next_msg_id(), temperature_c)
            await self._send_command(cmd, 3)
            self._protector_temperature = temperature_c

    async def calibrate(self, measured_temperature_c: float) -> None:
        """Calibrate the sensor against a measured reference temperature.

        Mirrors the app's ``calibrate``: the measured value is sent as the
        temperature the device should currently read.
        """
        cmd = commands.create_heater_calibrate_command(self.get_next_msg_id(), measured_temperature_c)
        await self._send_command(cmd, 3)

    async def reset_work_time(self) -> None:
        """Zero the runtime counter that drives the cleaning warning.

        Mirrors the app's ``resetWorkTime``, which is sent after the user
        cleans the heating tube; the new status arrives as a notification.
        """
        cmd = commands.create_heater_reset_work_time_command(self.get_next_msg_id())
        await self._send_command(cmd, 3)

    async def set_backlight(self, enabled: bool) -> None:
        """Turn the heater's display backlight on or off.

        The device does not report the setting, so the new state is tracked
        optimistically after the write succeeds.
        """
        async with self._state_lock:
            cmd = commands.create_heater_backlight_command(self.get_next_msg_id(), enabled=enabled)
            await self._send_command(cmd, 3)
            self._backlight = enabled
            self._backlight_schedule = None

    async def set_backlight_schedule(
        self,
        start_hour: int,
        start_minute: int,
        end_hour: int,
        end_minute: int,
        *,
        weekdays: int = 127,
    ) -> None:
        """Schedule the backlight, tracking the setting only after a successful write.

        Weekdays use Monday bit 6 through Sunday bit 0; 127 selects every day.
        No current on/off state is inferred from the clock or the weekday mask.
        """
        schedule = commands.HeaterBacklightSchedule(start_hour, start_minute, end_hour, end_minute, weekdays)
        async with self._state_lock:
            cmd = commands.create_heater_backlight_schedule_command(self.get_next_msg_id(), schedule)
            await self._send_command(cmd, 3)
            self._backlight = None
            self._backlight_schedule = schedule

    async def set_manual_state(self, temperature_c: float, power_watts: int) -> None:
        """Atomically set both manual values and switch the heater to manual mode."""
        async with self._state_lock:
            commands.split_heater_temperature(temperature_c)
            commands.encode_heater_power_watts(power_watts)
            await self.query_status()
            await self._set_manual_state_locked(temperature_c, power_watts)

    async def _set_manual_state_locked(self, temperature_c: float, power_watts: int) -> None:
        """Set and track both manual values while holding the state lock."""
        commands_to_send = [
            commands.create_switch_to_manual_mode_command(self.get_next_msg_id()),
            commands.create_heater_set_command(
                self.get_next_msg_id(),
                manual=True,
                temperature_c=temperature_c,
                power_watts=power_watts,
            ),
        ]
        power_sequence = self._power_notification_sequence
        await self._send_command(commands_to_send, 3, COMMAND_NOTIFICATION_WAIT)
        self._setting_temperature = temperature_c
        if self._power_notification_sequence == power_sequence:
            self._power_watts = power_watts

    async def set_manual_mode(self) -> None:
        """Switch the heater to manual mode without changing its state."""
        cmd = commands.create_switch_to_manual_mode_command(self.get_next_msg_id())
        await self._send_command(cmd, 3)

    async def query_status(self) -> None:
        """Request runtime, alarms and configured power; measurements arrive periodically."""
        cmd = commands.create_query_status_command(self.get_next_msg_id())
        await self._send_command(cmd, 3, notification_wait=STATUS_NOTIFICATION_WAIT)


__all__ = ["ChihirosHeater"]
