"""Heater family driver."""

from __future__ import annotations

import asyncio

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from ..models import DeviceModel
from ..protocol import heater as commands
from ..protocol.heater import heater_alarm_names
from ..protocol.notifications import HeaterStatusNotification, HeaterTemperatureNotification, ParsedNotification
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
        self._protector_temperature = commands.HEATER_DEFAULT_PROTECTOR_TEMPERATURE_C
        self._auto_default_temperature = commands.HEATER_DEFAULT_AUTO_TEMPERATURE_C
        self._auto_default_power_watts = commands.HEATER_DEFAULT_AUTO_POWER_WATTS
        self._auto_heating = False
        self._celsius = True
        self._backlight = True
        self.last_heater_temperature_notification: HeaterTemperatureNotification | None = None
        self.last_heater_status_notification: HeaterStatusNotification | None = None

    def _parse_notification(self, data: bytes | bytearray) -> ParsedNotification | None:
        """Parse heater temperature and status notifications."""
        return commands.parse_notification(data)

    def _record_notification(self, parsed: ParsedNotification) -> None:
        """Record a notification, folding the reported setting temperature in.

        The device is authoritative for the target temperature the next
        ``set_power`` has to resend, so a temperature frame updates the tracked
        setting as well as the last-seen notification.
        """
        super()._record_notification(parsed)
        if isinstance(parsed, HeaterTemperatureNotification):
            self.last_heater_temperature_notification = parsed
            self._setting_temperature = parsed.setting_temperature_celsius
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
    def power_watts(self) -> int:
        """Return the tracked manual power in watts."""
        return self._power_watts

    @property
    def protector_temperature_celsius(self) -> float:
        """Return the tracked overheat protection temperature."""
        return self._protector_temperature

    @property
    def auto_default_temperature_celsius(self) -> float:
        """Return the tracked auto-mode default temperature."""
        return self._auto_default_temperature

    @property
    def auto_default_power_watts(self) -> int:
        """Return the tracked auto-mode default power in watts."""
        return self._auto_default_power_watts

    @property
    def auto_heating(self) -> bool:
        """Return whether auto heating is enabled."""
        return self._auto_heating

    @property
    def is_celsius(self) -> bool:
        """Return whether the device displays Celsius (as opposed to Fahrenheit)."""
        return self._celsius

    @property
    def backlight(self) -> bool:
        """Return whether the tracked display-backlight state is on."""
        return self._backlight

    @property
    def work_time_hours(self) -> int | None:
        """Return the heating runtime since the last cleaning reset, if reported."""
        notification = self.last_heater_status_notification
        return notification.work_time_hours if notification else None

    @property
    def heater_alarms(self) -> tuple[str, ...]:
        """Return the names of the alarms in the last status frame."""
        notification = self.last_heater_status_notification
        return heater_alarm_names(notification.alarms) if notification else ()

    @property
    def firmware_version(self) -> int | None:
        """Return the firmware version of the last status frame, if any."""
        notification = self.last_heater_status_notification
        return notification.firmware_version if notification else None

    async def set_temperature(self, temperature_c: float) -> None:
        """Switch to manual mode and set the target temperature.

        Mirrors the app's ``setTemperature`` → ``initManual`` sequence: one
        paced batch with ``switchToManual`` followed by the state frame
        carrying the currently tracked power.
        """
        async with self._state_lock:
            await self._set_manual_state_locked(temperature_c, self._power_watts)

    async def set_power(self, power_watts: int) -> None:
        """Switch to manual mode and set the power in watts."""
        async with self._state_lock:
            await self._set_manual_state_locked(self._setting_temperature, power_watts)

    async def set_auto_defaults(self, temperature_c: float, power_watts: int) -> None:
        """Set the auto-mode defaults the scene schedules heat towards."""
        async with self._state_lock:
            await self._set_auto_defaults_locked(temperature_c, power_watts)

    async def set_auto_default_temperature(self, temperature_c: float) -> None:
        """Set the auto-mode default temperature, resending the tracked power."""
        async with self._state_lock:
            await self._set_auto_defaults_locked(temperature_c, self._auto_default_power_watts)

    async def set_auto_default_power(self, power_watts: int) -> None:
        """Set the auto-mode default power, resending the tracked temperature."""
        async with self._state_lock:
            await self._set_auto_defaults_locked(self._auto_default_temperature, power_watts)

    def restore_setting_temperature(self, temperature_c: float) -> None:
        """Restore the tracked manual target temperature without writing to the device."""
        commands.split_heater_temperature(temperature_c)
        self._setting_temperature = temperature_c

    def restore_manual_power(self, power_watts: int) -> None:
        """Restore tracked manual power without writing to the device."""
        commands.encode_heater_power_watts(power_watts)
        self._power_watts = power_watts

    def restore_auto_default_temperature(self, temperature_c: float) -> None:
        """Restore the tracked auto temperature without writing to the device."""
        commands.split_heater_temperature(temperature_c)
        self._auto_default_temperature = temperature_c

    def restore_auto_default_power(self, power_watts: int) -> None:
        """Restore tracked auto power without writing to the device."""
        commands.encode_heater_power_watts(power_watts)
        self._auto_default_power_watts = power_watts

    async def set_auto_mode(self) -> None:
        """Switch the heater to auto mode (app's ``switchToAuto``)."""
        cmd = commands.create_heater_auto_mode_command(self.get_next_msg_id())
        await self._send_command(cmd, 3)

    async def apply_scene(self) -> None:
        """Apply the stored scene/auto schedule (app's ``switchToScene``)."""
        cmd = commands.create_heater_scene_command(self.get_next_msg_id())
        await self._send_command(cmd, 3)

    async def set_auto_heating(self, enabled: bool) -> None:
        """Enable or disable the heating element in auto mode (app's ``setHeaterAuto``)."""
        async with self._state_lock:
            cmd = commands.create_heater_auto_heating_command(self.get_next_msg_id(), enabled)
            await self._send_command(cmd, 3)
            self._auto_heating = enabled

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

    async def set_manual_state(self, temperature_c: float, power_watts: int) -> None:
        """Atomically set both manual values and switch the heater to manual mode."""
        async with self._state_lock:
            await self._set_manual_state_locked(temperature_c, power_watts)

    async def _set_manual_state_locked(self, temperature_c: float, power_watts: int) -> None:
        """Set and track both manual values while holding the state lock."""
        commands_to_send = [
            commands.create_switch_to_manual_mode_command(self.get_next_msg_id()),
            commands.create_heater_set_command(
                self.get_next_msg_id(),
                auto=False,
                temperature_c=temperature_c,
                power_watts=power_watts,
            ),
        ]
        await self._send_command(commands_to_send, 3, COMMAND_NOTIFICATION_WAIT)
        self._setting_temperature = temperature_c
        self._power_watts = power_watts

    async def _set_auto_defaults_locked(self, temperature_c: float, power_watts: int) -> None:
        """Set and track both auto defaults while holding the state lock."""
        cmd = commands.create_heater_set_command(
            self.get_next_msg_id(),
            auto=True,
            temperature_c=temperature_c,
            power_watts=power_watts,
        )
        await self._send_command([bytes(cmd)], 3, COMMAND_NOTIFICATION_WAIT)
        self._auto_default_temperature = temperature_c
        self._auto_default_power_watts = power_watts

    async def set_manual_mode(self) -> None:
        """Switch the heater to manual mode without changing its state."""
        cmd = commands.create_switch_to_manual_mode_command(self.get_next_msg_id())
        await self._send_command(cmd, 3)

    async def query_status(self) -> None:
        """Request the heater's temperature/status notification snapshot."""
        cmd = commands.create_query_status_command(self.get_next_msg_id())
        await self._send_command(cmd, 3, notification_wait=STATUS_NOTIFICATION_WAIT)


__all__ = ["ChihirosHeater"]
