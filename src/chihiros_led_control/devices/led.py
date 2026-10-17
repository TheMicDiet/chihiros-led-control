"""LED family driver."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from ..models import (
    DeviceModel,
    LedControllerMode,
    LedFeature,
    LedSpec,
    replay_auto_on_reconnect,
    supports_weekdays,
)
from ..protocol import led as commands
from ..protocol.frame import create_command_encoding
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


@dataclass(frozen=True)
class LedSchedule:
    """One scene period; brightness uses the model's raw channel units."""

    sunrise: datetime
    sunset: datetime
    max_brightness: int | Sequence[int] | Mapping[str | int, int] = 100
    ramp_up_in_minutes: int = 0
    weekdays: Sequence[WeekdaySelect] | None = None


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
        self._controller_mode = LedControllerMode.SCENE
        self._auto_mode = False
        self._owned_schedule: tuple[bytes, ...] | None = None

    @property
    def controller_mode(self) -> LedControllerMode:
        """Return the selected automatic controller independently of power mode."""
        return self._controller_mode

    @property
    def firmware_version(self) -> int | None:
        """Return the last firmware version reported by the controller."""
        for notification in (
            self.last_runtime_notification,
            self.last_fan_status_notification,
            self.last_schedule_snapshot_notification,
        ):
            if notification is not None:
                return notification.firmware_version
        return None

    @property
    def supports_weekdays(self) -> bool:
        """Return whether the app exposes a weekday selector for this device."""
        return self.model.supports_weekdays and supports_weekdays(self.name)

    def _validate_scene_support(self) -> None:
        """Reject scenes when capability or known firmware rules them out."""
        spec = self.model.spec
        if not isinstance(spec, LedSpec) or spec.scene_min_firmware is None:
            raise ValueError(f"Model does not support scenes: {self.model.name}")
        firmware = self.firmware_version
        if firmware is not None and firmware < spec.scene_min_firmware:
            raise ValueError(f"Scenes require firmware {spec.scene_min_firmware} or newer")

    def _scene_weekdays(self, weekdays: Sequence[WeekdaySelect] | None) -> int:
        """Encode weekdays without advertising an unsupported selector."""
        selected = list(weekdays) if weekdays is not None else [WeekdaySelect.everyday]
        encoded = encode_selected_weekdays(selected)
        if not self.supports_weekdays and encoded != 127:
            raise ValueError(f"Model does not support weekday selection: {self.model.name}")
        return encoded

    def _schedule_setup(self) -> list[bytes]:
        """Build the reset/mode pair for the explicitly selected controller."""
        if self._controller_mode is LedControllerMode.SCENE:
            reset = commands.create_reset_scene_settings_command
            switch = commands.create_switch_to_scene_mode_command
        else:
            reset = commands.create_reset_auto_settings_command
            switch = commands.create_switch_to_auto_mode_command
        return [bytes(reset(self.get_next_msg_id())), bytes(switch(self.get_next_msg_id()))]

    def _replay_schedule(self) -> list[bytes]:
        """Rebuild fresh message IDs for authoritative wire payloads."""
        assert self._owned_schedule is not None  # nosec
        scene = self._controller_mode is LedControllerMode.SCENE
        return [
            bytes(
                create_command_encoding(165 if scene else 90, 25 if scene else 6, self.get_next_msg_id(), list(payload))
            )
            for payload in self._owned_schedule
        ]

    async def _connection_prelude(self) -> Sequence[bytes]:
        """Replay library-owned Lite auto schedules even when unchanged."""
        prelude = list(await super()._connection_prelude())
        lite = self.model.replay_auto_on_reconnect or replay_auto_on_reconnect(self.name)
        if lite and self._auto_mode and self._owned_schedule is not None:
            prelude.extend(self._schedule_setup())
            prelude.extend(self._replay_schedule())
        return prelude

    def _parse_notification(self, data: bytes | bytearray) -> ParsedNotification | None:
        """Parse LED and accessory notifications."""
        parsed = commands.parse_notification(data, self.model.spec)
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

    def _normalize_brightness(self, brightness: int | Sequence[int] | Mapping[str | int, int]) -> dict[int, int]:
        """Normalize raw model-unit brightness inputs to protocol channel ids."""
        if isinstance(brightness, int):
            return self._normalize_brightness_mapping({self._primary_schedule_color(): brightness})
        if isinstance(brightness, Mapping):
            return self._normalize_brightness_mapping(brightness)
        return self._normalize_brightness_sequence(brightness)

    def _normalize_brightness_sequence(self, brightness: Sequence[int]) -> dict[int, int]:
        """Resolve ordered raw levels to the primary channel or all channel slots."""
        brightness_values = list(brightness)
        if not brightness_values:
            raise ValueError("At least one brightness level is required")
        channel_count = self._channel_count()
        if len(brightness_values) == 1:
            return self._normalize_brightness(brightness_values[0])
        if len(brightness_values) != channel_count:
            raise ValueError(f"Expected 1 or {channel_count} brightness levels")
        return self._normalize_brightness_mapping(dict(enumerate(brightness_values)))

    def _normalize_brightness_mapping(self, brightness: Mapping[str | int, int]) -> dict[int, int]:
        """Normalize mapping-style brightness input to protocol channel ids."""
        if not brightness:
            raise ValueError("At least one brightness level is required")
        result: dict[int, int] = {}
        for color, level in brightness.items():
            color_id = self._color_id(color)
            if color_id is None:
                raise ValueError(f"Color not supported: {color}")
            self.model.spec.validate_level(color_id, level)
            result[color_id] = level
        return result

    def _channel_count(self) -> int:
        """Return number of protocol channel slots for this model."""
        return max(self.model.color_channels.values()) + 1

    def _brightness_parameter_values(self, brightness: int | Sequence[int] | Mapping[str | int, int]) -> list[int]:
        """Return auto schedule brightness parameters ordered by channel id."""
        brightness_by_channel = self._normalize_brightness(brightness)
        return [
            self.model.spec.to_wire_level(channel_id, brightness_by_channel[channel_id])
            if channel_id in brightness_by_channel
            else 255
            for channel_id in range(self._channel_count())
        ]

    async def set_brightness(self, brightness: int | Sequence[int] | Mapping[str | int, int]) -> None:
        """Switch to manual mode and set brightness in raw model units.

        The vendor app sends ``switchToManual()`` before manual slider writes;
        keeping both in one paced transaction avoids racing auto mode.
        """
        brightness_by_channel = self._normalize_brightness(brightness)
        commands_to_send = [
            commands.create_switch_to_manual_mode_command(self.get_next_msg_id()),
            *(
                commands.create_set_brightness_command(
                    self.get_next_msg_id(), color_id, self.model.spec.to_wire_level(color_id, brightness_level)
                )
                for color_id, brightness_level in brightness_by_channel.items()
            ),
        ]
        await self._send_command(commands_to_send, 3)
        self._auto_mode = False

    def _primary_schedule_color(self) -> str:
        """Return the single channel used by plain auto schedules."""
        if "white" in self.model.color_channels:
            return "white"
        return min(self.model.color_channels, key=self.model.color_channels.__getitem__)

    async def turn_on(self) -> None:
        """Turn on the light."""
        await self.set_brightness(
            {
                color_name: self.model.spec.max_level(channel)
                for color_name, channel in self.model.color_channels.items()
            }
        )

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
        """Add a scene in raw model units without claiming full schedule authority."""
        self._validate_scene_support()
        brightness = self._brightness_parameter_values(max_brightness)
        cmd = commands.create_add_auto_setting_command(
            self.get_next_msg_id(),
            sunrise.time(),
            sunset.time(),
            brightness,
            ramp_up_in_minutes,
            self._scene_weekdays(weekdays),
        )
        self._owned_schedule = None
        self._controller_mode = LedControllerMode.SCENE
        await self._send_command(cmd, 3)

    async def replace_settings(self, settings: Sequence[LedSchedule]) -> None:
        """Replace all scenes in one paced transaction and retain replay authority."""
        self._validate_scene_support()
        entries = [
            commands.create_add_auto_setting_command(
                self.get_next_msg_id(),
                setting.sunrise.time(),
                setting.sunset.time(),
                self._brightness_parameter_values(setting.max_brightness),
                setting.ramp_up_in_minutes,
                self._scene_weekdays(setting.weekdays),
            )
            for setting in settings
        ]
        self._owned_schedule = None
        self._controller_mode = LedControllerMode.SCENE
        batch = [commands.create_set_time_command(self.get_next_msg_id()), *self._schedule_setup(), *entries]
        await self._send_command(batch, 3)
        self._owned_schedule = tuple(bytes(entry[6:-1]) for entry in entries)
        self._auto_mode = True

    async def remove_setting(
        self,
        sunrise: datetime,
        sunset: datetime,
        ramp_up_in_minutes: int = 0,
        weekdays: list[WeekdaySelect] | None = None,
    ) -> None:
        """Remove a scene and relinquish authority over the remaining periods."""
        self._validate_scene_support()
        cmd = commands.create_delete_auto_setting_command(
            self.get_next_msg_id(),
            sunrise.time(),
            sunset.time(),
            ramp_up_in_minutes,
            self._scene_weekdays(weekdays),
            brightness_channels=self._channel_count(),
        )
        self._owned_schedule = None
        self._controller_mode = LedControllerMode.SCENE
        await self._send_command(cmd, 3)

    async def reset_settings(self, *, controller_mode: LedControllerMode | None = None) -> None:
        """Clear scenes or custom curves without conflating their reset commands."""
        mode = self._controller_mode if controller_mode is None else controller_mode
        if mode is LedControllerMode.SCENE:
            self._validate_scene_support()
        self._controller_mode = mode
        self._owned_schedule = None
        reset = (
            commands.create_reset_scene_settings_command
            if self._controller_mode is LedControllerMode.SCENE
            else commands.create_reset_auto_settings_command
        )
        cmd = reset(self.get_next_msg_id())
        await self._send_command(cmd, 3)

    async def set_auto_point(self, channel: int, minutes: int, level: int) -> None:
        """Write one auto-curve point (``0x5A, 6``) in raw model units.

        Time encoding follows the model family (see ``create_auto_point_command``);
        prefer :meth:`set_auto_curve` when writing more than one point.
        """
        self._validate_auto_point(channel, minutes, level)
        cmd = commands.create_auto_point_command(
            self.get_next_msg_id(),
            channel,
            minutes,
            self.model.spec.to_wire_level(channel, level),
            protocol=self.model.spec.protocol,
        )
        self._owned_schedule = None
        self._controller_mode = LedControllerMode.CUSTOM_CURVE
        await self._send_command(cmd, 3)

    async def set_auto_curve(self, points: Sequence[tuple[int, int, int]]) -> None:
        """Replace raw model-unit points using reset, custom mode and paced writes."""
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
                    self.model.spec.to_wire_level(channel, level),
                    protocol=self.model.spec.protocol,
                )
            )
            for channel, minutes, level in points
        ]
        self._owned_schedule = None
        self._controller_mode = LedControllerMode.CUSTOM_CURVE
        commands_to_send = [*self._schedule_setup(), *commands_to_send]
        await self._send_command(commands_to_send, 3)
        self._owned_schedule = tuple(bytes(frame[6:-1]) for frame in commands_to_send[2:])
        self._auto_mode = True

    def _validate_auto_point(self, channel: int, minutes: int, level: int) -> None:
        """Validate one auto-curve point against this model."""
        if not self.model.color_channels:
            raise ValueError(f"Model does not support auto curve points: {self.model.name}")
        if channel not in self.model.color_channels.values():
            raise ValueError(f"Channel must be between 0 and {self._channel_count() - 1}")
        if not 0 <= minutes <= commands.AUTO_POINT_MAX_MINUTES:
            raise ValueError(f"Minutes must be between 0 and {commands.AUTO_POINT_MAX_MINUTES}")
        self.model.spec.validate_level(channel, level)

    async def enable_auto_mode(
        self,
        timestamp: datetime | None = None,
        *,
        controller_mode: LedControllerMode | None = None,
    ) -> None:
        """Sync time and enable scenes or explicitly selected custom curves."""
        if controller_mode is not None and controller_mode is not self._controller_mode:
            self._owned_schedule = None
            self._controller_mode = controller_mode
        if self._controller_mode is LedControllerMode.SCENE:
            self._validate_scene_support()
            switch = commands.create_switch_to_scene_mode_command
        else:
            switch = commands.create_switch_to_auto_mode_command
        time_cmd = commands.create_set_time_command(self.get_next_msg_id(), timestamp)
        lite = self.model.replay_auto_on_reconnect or replay_auto_on_reconnect(self.name)
        if lite and self._owned_schedule is not None:
            # This batch owns initialization; suppress a duplicate prelude replay.
            self._auto_mode = False
            await self._send_command([time_cmd, *self._schedule_setup(), *self._replay_schedule()], 3)
            self._auto_mode = True
            return
        switch_cmd = switch(self.get_next_msg_id())
        await self._send_command(time_cmd, 3)
        await self._send_command(switch_cmd, 3)
        self._auto_mode = True

    async def set_manual_mode(self) -> None:
        """Switch to manual mode without changing brightness."""
        cmd = commands.create_switch_to_manual_mode_command(self.get_next_msg_id())
        await self._send_command(cmd, 3)
        self._auto_mode = False


__all__ = ["ChihirosDevice", "LedSchedule"]
