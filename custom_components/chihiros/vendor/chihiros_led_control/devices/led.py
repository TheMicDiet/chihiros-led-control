"""LED family driver."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from ..models import DeviceModel, LedFeature, LedSpec
from ..protocol import led as commands
from ..protocol.notifications import (
    FanStatusNotification,
    ParsedNotification,
    RuntimeNotification,
    ScheduleSnapshotNotification,
)
from ..registry import FALLBACK
from ..transport import ChihirosTransport
from ..weekday_encoding import WeekdaySelect, encode_selected_weekdays
from .base import STATUS_NOTIFICATION_WAIT, BaseChihirosDevice


class ChihirosDevice(BaseChihirosDevice):
    """Concrete BLE client for LED devices."""

    def __init__(
        self,
        ble_device: BLEDevice,
        model: DeviceModel = FALLBACK,
        advertisement_data: AdvertisementData | None = None,
        *,
        transport: ChihirosTransport | None = None,
    ) -> None:
        """Create an LED device and initialize its optimistic optional state."""
        super().__init__(ble_device, model, advertisement_data, transport=transport)
        self._fan_auto = False
        self._fan_start_temp = 38
        self._fan_stop_temp = 33
        self._temp_protect = False
        self._bluetooth_led = False
        self.last_runtime_notification: RuntimeNotification | None = None
        self.last_fan_status_notification: FanStatusNotification | None = None
        self.last_schedule_snapshot_notification: ScheduleSnapshotNotification | None = None

    def _parse_notification(self, data: bytes | bytearray) -> ParsedNotification | None:
        """Parse LED and accessory notifications."""
        parsed = commands.parse_notification(data, self.model.color_channels)
        if isinstance(parsed, FanStatusNotification) and (
            not isinstance(self.model.spec, LedSpec) or LedFeature.FAN not in self.model.spec.features
        ):
            self._logger.debug("%s: Ignoring fan readout frame on non-fan model %s", self.name, self.model.name)
            return None
        return parsed

    def _record_notification(self, parsed: ParsedNotification) -> None:
        """Store the last LED notification and log its values."""
        if isinstance(parsed, RuntimeNotification):
            self.last_runtime_notification = parsed
            self._logger.debug(
                "%s: Runtime notification received; firmware=%s runtime_minutes=%s",
                self.name,
                parsed.firmware_version,
                parsed.runtime_minutes,
            )
        elif isinstance(parsed, FanStatusNotification):
            self.last_fan_status_notification = parsed
            self._logger.debug(
                "%s: Fan status notification received; firmware=%s fan_rpm=%s temperature_celsius=%s",
                self.name,
                parsed.firmware_version,
                parsed.fan_rpm,
                parsed.temperature_celsius,
            )
        elif isinstance(parsed, ScheduleSnapshotNotification):
            self.last_schedule_snapshot_notification = parsed
            self._logger.debug(
                "%s: Schedule snapshot notification received; firmware=%s points=%s",
                self.name,
                parsed.firmware_version,
                parsed.points,
            )

    @property
    def colors(self) -> dict[str, int]:
        """Return supported LED channel names and protocol IDs."""
        return dict(self.model.color_channels)

    def _color_id(self, color: str | int) -> int | None:
        """Return protocol channel id for a color name or id."""
        color_id: int | None = None
        colors = self.model.color_channels
        if isinstance(color, int) and color in colors.values():
            color_id = color
        elif isinstance(color, str) and color in colors:
            color_id = colors[color]
        return color_id

    def _validate_brightness_levels(self, brightness: Sequence[int]) -> None:
        """Validate brightness levels."""
        if not brightness:
            raise ValueError("At least one brightness level is required")
        if any(level < 0 or level > 100 for level in brightness):
            raise ValueError("Brightness levels must be between 0 and 100")

    def _normalize_brightness(self, brightness: int | Sequence[int] | Mapping[str | int, int]) -> dict[int, int]:
        """Normalize supported brightness inputs to protocol channel ids."""
        if isinstance(brightness, int):
            color_id = self._color_id(self._primary_schedule_color())
            assert color_id is not None  # nosec
            self._validate_brightness_levels((brightness,))
            return {color_id: brightness}
        if isinstance(brightness, Mapping):
            return self._normalize_brightness_mapping(brightness)

        brightness_values = list(brightness)
        self._validate_brightness_levels(brightness_values)
        channel_count = self._channel_count()
        if len(brightness_values) == 1:
            color_id = self._color_id(self._primary_schedule_color())
            assert color_id is not None  # nosec
            return {color_id: brightness_values[0]}
        if len(brightness_values) != channel_count:
            raise ValueError(f"Expected 1 or {channel_count} brightness levels")
        return dict(enumerate(brightness_values))

    def _normalize_brightness_mapping(self, brightness: Mapping[str | int, int]) -> dict[int, int]:
        """Normalize mapping-style brightness input to protocol channel ids."""
        self._validate_brightness_levels(tuple(brightness.values()))
        result: dict[int, int] = {}
        for color, level in brightness.items():
            color_id = self._color_id(color)
            if color_id is None:
                raise ValueError(f"Color not supported: {color}")
            result[color_id] = level
        return result

    def _channel_count(self) -> int:
        """Return number of protocol channel slots for this model."""
        return max(self.model.color_channels.values()) + 1

    def _brightness_parameter_values(self, brightness: int | Sequence[int] | Mapping[str | int, int]) -> list[int]:
        """Return auto schedule brightness parameters ordered by channel id."""
        brightness_by_channel = self._normalize_brightness(brightness)
        return [brightness_by_channel.get(channel_id, 255) for channel_id in range(self._channel_count())]

    async def set_brightness(self, brightness: int | Sequence[int] | Mapping[str | int, int]) -> None:
        """Switch to manual mode and set light brightness.

        The vendor app sends ``switchToManual()`` before manual slider writes;
        keeping both in one paced transaction avoids racing auto mode.
        """
        brightness_by_channel = self._normalize_brightness(brightness)
        commands_to_send = [
            commands.create_switch_to_manual_mode_command(self.get_next_msg_id()),
            *(
                commands.create_set_brightness_command(self.get_next_msg_id(), color_id, brightness_level)
                for color_id, brightness_level in brightness_by_channel.items()
            ),
        ]
        await self._send_command(commands_to_send, 3)

    def _primary_schedule_color(self) -> str:
        """Return the single channel used by plain auto schedules."""
        if "white" in self.model.color_channels:
            return "white"
        return min(self.model.color_channels, key=self.model.color_channels.__getitem__)

    async def turn_on(self) -> None:
        """Turn on the light."""
        await self.set_brightness({color_name: 100 for color_name in self.model.color_channels})

    async def turn_off(self) -> None:
        """Turn off the light."""
        await self.set_brightness({color_name: 0 for color_name in self.model.color_channels})

    async def query_status(self) -> None:
        """Ask the device to send its runtime/status notification snapshot."""
        cmd = commands.create_query_status_command(self.get_next_msg_id())
        await self._send_command(cmd, 3, notification_wait=STATUS_NOTIFICATION_WAIT)

    async def set_fan_speed(self, speed_percent: int) -> None:
        """Set the fan speed percentage on fan-equipped models.

        Values below the model's minimum are clamped to it (the app clamps the
        VIVID III to 25 %). Manual speed leaves temperature auto mode.
        """
        if not isinstance(self.model.spec, LedSpec) or LedFeature.FAN not in self.model.spec.features:
            raise ValueError(f"Model does not support fan control: {self.model.name}")
        if speed_percent < 0 or speed_percent > 100:
            raise ValueError("Fan speed must be between 0 and 100 percent")
        if 0 < speed_percent < self.model.min_fan_speed:
            speed_percent = self.model.min_fan_speed
        cmd = commands.create_set_fan_speed_command(self.get_next_msg_id(), speed_percent)
        await self._send_command(cmd, 3)
        self._fan_auto = False

    async def set_fan_auto(self) -> None:
        """Switch the fan to temperature-controlled auto mode.

        Matches the vendor app's ``LedInfo::setFanAuto()`` frame; the fan then
        starts/stops from the configured start/stop temperatures.
        """
        if not isinstance(self.model.spec, LedSpec) or LedFeature.FAN not in self.model.spec.features:
            raise ValueError(f"Model does not support fan control: {self.model.name}")
        cmd = commands.create_fan_auto_mode_command(self.get_next_msg_id())
        await self._send_command(cmd, 3)
        self._fan_auto = True

    async def set_fan_start_stop_temp(self, start_temp: int, stop_temp: int) -> None:
        """Set the VIVID3 fan start/stop temperatures used by auto mode."""
        if not isinstance(self.model.spec, LedSpec) or LedFeature.FAN not in self.model.spec.features:
            raise ValueError(f"Model does not support fan control: {self.model.name}")
        cmd = commands.create_vivid3_fan_start_stop_temp_command(
            self.get_next_msg_id(),
            start_temp,
            stop_temp,
        )
        await self._send_command(cmd, 3)
        self._fan_start_temp = start_temp
        self._fan_stop_temp = stop_temp

    async def set_temp_protect(self, enabled: bool) -> None:
        """Toggle the VIVID3 temperature protection.

        Matches the vendor app's ``Vivid3Info::tempProtect()`` frame. The device
        sends no acknowledgement, so the new state is tracked optimistically.
        """
        if (
            not isinstance(self.model.spec, LedSpec)
            or LedFeature.TEMPERATURE_PROTECTION not in self.model.spec.features
        ):
            raise ValueError(f"Model does not support temperature protection: {self.model.name}")
        cmd = commands.create_vivid3_temp_protect_command(self.get_next_msg_id(), enabled)
        await self._send_command(cmd, 3)
        self._temp_protect = enabled

    async def set_bluetooth_led(self, enabled: bool) -> None:
        """Toggle the VIVID3 indicator LED.

        Matches the vendor app's ``Vivid3Info::setLed()`` frame. The device
        sends no acknowledgement, so the new state is tracked optimistically.
        """
        if not isinstance(self.model.spec, LedSpec) or LedFeature.INDICATOR_LED not in self.model.spec.features:
            raise ValueError(f"Model does not support the indicator LED switch: {self.model.name}")
        cmd = commands.create_vivid3_bluetooth_led_command(self.get_next_msg_id(), enabled)
        await self._send_command(cmd, 3)
        self._bluetooth_led = enabled

    @property
    def temp_protect(self) -> bool:
        """Return the optimistically tracked temperature-protection state."""
        return self._temp_protect

    @property
    def bluetooth_led(self) -> bool:
        """Return the optimistically tracked indicator-LED state."""
        return self._bluetooth_led

    @property
    def fan_auto(self) -> bool:
        """Return whether the fan is in temperature-controlled auto mode."""
        return self._fan_auto

    @property
    def fan_start_temp(self) -> int:
        """Return the last fan start temperature in whole degrees Celsius."""
        return self._fan_start_temp

    @property
    def fan_stop_temp(self) -> int:
        """Return the last fan stop temperature in whole degrees Celsius."""
        return self._fan_stop_temp

    async def add_setting(
        self,
        sunrise: datetime,
        sunset: datetime,
        max_brightness: int | Sequence[int] | Mapping[str | int, int] = 100,
        ramp_up_in_minutes: int = 0,
        weekdays: list[WeekdaySelect] | None = None,
    ) -> None:
        """Add an automation setting to the light."""
        if weekdays is None:
            weekdays = [WeekdaySelect.everyday]
        brightness = self._brightness_parameter_values(max_brightness)
        cmd = commands.create_add_auto_setting_command(
            self.get_next_msg_id(),
            sunrise.time(),
            sunset.time(),
            brightness,
            ramp_up_in_minutes,
            encode_selected_weekdays(weekdays),
        )
        await self._send_command(cmd, 3)

    async def remove_setting(
        self,
        sunrise: datetime,
        sunset: datetime,
        ramp_up_in_minutes: int = 0,
        weekdays: list[WeekdaySelect] | None = None,
    ) -> None:
        """Remove an automation setting from the light."""
        if weekdays is None:
            weekdays = [WeekdaySelect.everyday]
        cmd = commands.create_delete_auto_setting_command(
            self.get_next_msg_id(),
            sunrise.time(),
            sunset.time(),
            ramp_up_in_minutes,
            encode_selected_weekdays(weekdays),
            brightness_channels=self._channel_count(),
        )
        await self._send_command(cmd, 3)

    async def reset_settings(self) -> None:
        """Remove all automation settings from the light."""
        cmd = commands.create_reset_auto_settings_command(self.get_next_msg_id())
        await self._send_command(cmd, 3)

    async def set_auto_point(self, channel: int, minutes: int, level: int) -> None:
        """Write one auto-curve point (``0x5A, 6``) for a channel.

        Time encoding follows the model family (see ``create_auto_point_command``);
        prefer :meth:`set_auto_curve` when writing more than one point.
        """
        self._validate_auto_point(channel, minutes, level)
        cmd = commands.create_auto_point_command(
            self.get_next_msg_id(),
            channel,
            minutes,
            level,
            protocol=self.model.spec.protocol,
        )
        await self._send_command(cmd, 3)

    async def set_auto_curve(self, points: Sequence[tuple[int, int, int]]) -> None:
        """Replace the auto curve with ``0x5A, 6`` points in one paced transaction.

        Call :meth:`reset_settings` first to clear the stored curve (the app
        sends ``0x5A, 5, [5, 255, 255]`` before re-applying a saved curve).
        """
        if not points:
            raise ValueError("Auto curve must contain at least one point")
        for channel, minutes, level in points:
            self._validate_auto_point(channel, minutes, level)
        commands_to_send = [
            bytes(
                commands.create_auto_point_command(
                    self.get_next_msg_id(),
                    channel,
                    minutes,
                    level,
                    protocol=self.model.spec.protocol,
                )
            )
            for channel, minutes, level in points
        ]
        await self._send_command(commands_to_send, 3)

    def _validate_auto_point(self, channel: int, minutes: int, level: int) -> None:
        """Validate one auto-curve point against this model."""
        if not self.model.color_channels:
            raise ValueError(f"Model does not support auto curve points: {self.model.name}")
        if not 0 <= channel < self._channel_count():
            raise ValueError(f"Channel must be between 0 and {self._channel_count() - 1}")

    async def enable_auto_mode(self, timestamp: datetime | None = None) -> None:
        """Enable auto mode."""
        time_cmd = commands.create_set_time_command(self.get_next_msg_id(), timestamp)
        switch_cmd = commands.create_switch_to_auto_mode_command(self.get_next_msg_id())
        await self._send_command(time_cmd, 3)
        await self._send_command(switch_cmd, 3)

    async def set_manual_mode(self) -> None:
        """Switch to manual mode without changing brightness."""
        cmd = commands.create_switch_to_manual_mode_command(self.get_next_msg_id())
        await self._send_command(cmd, 3)


__all__ = ["ChihirosDevice"]
