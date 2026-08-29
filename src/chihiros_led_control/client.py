"""Runtime BLE client for Chihiros LED devices."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.backends.service import (
    BleakGATTCharacteristic,  # type: ignore
    BleakGATTServiceCollection,
)
from bleak.exc import BleakDBusError
from bleak_retry_connector import BLEAK_RETRY_EXCEPTIONS as BLEAK_EXCEPTIONS
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakError,  # type: ignore
    BleakNotFoundError,
    establish_connection,
)

from . import commands
from .const import (
    HM10_RX_CHAR_UUID,
    HM10_TX_CHAR_UUID,
    UART_RX_CHAR_UUID,
    UART_TX_CHAR_UUID,
)
from .exceptions import CharacteristicMissingError
from .models import FALLBACK, DeviceModel
from .protocol import (
    DosingDailyNotification,
    DosingTotalsNotification,
    FanStatusNotification,
    ParsedNotification,
    RuntimeNotification,
    ScheduleSnapshotNotification,
    Vivid3FanStatusNotification,
    next_message_id,
    parse_notification,
)
from .weekday_encoding import WeekdaySelect, encode_selected_weekdays

DEFAULT_ATTEMPTS = 3
BLEAK_BACKOFF_TIME = 0.25
COMMAND_NOTIFICATION_WAIT = 0.5
STATUS_NOTIFICATION_WAIT = 1.0
# Vendor app paces frames inside one command batch 30 ms apart.
BATCH_WRITE_DELAY = 0.03
NotificationCallback = Callable[[ParsedNotification], None]

# Per-notification-type: last-seen attribute, debug log message, and the
# parsed fields interpolated into that message.
_LAST_NOTIFICATION_FIELDS: dict[type, tuple[str, str, tuple[str, ...]]] = {
    RuntimeNotification: (
        "last_runtime_notification",
        "Runtime notification received; firmware=%s runtime_minutes=%s",
        ("firmware_version", "runtime_minutes"),
    ),
    FanStatusNotification: (
        "last_fan_status_notification",
        "Fan status notification received; firmware=%s fan_rpm=%s temperature_celsius=%s",
        ("firmware_version", "fan_rpm", "temperature_celsius"),
    ),
    ScheduleSnapshotNotification: (
        "last_schedule_snapshot_notification",
        "Schedule snapshot notification received; firmware=%s points=%s",
        ("firmware_version", "points"),
    ),
    DosingTotalsNotification: (
        "last_dosing_totals_notification",
        "Dosing totals notification received; total_dosed_ul=%s",
        ("total_dosed_ul",),
    ),
    DosingDailyNotification: (
        "last_dosing_daily_notification",
        "Dosing daily notification received; dose_use_in_day_ul=%s",
        ("dose_use_in_day_ul",),
    ),
    Vivid3FanStatusNotification: (
        "last_vivid3_fan_status_notification",
        "VIVID3 fan notification received; fan_rpm=%s temperature_celsius=%s",
        ("fan_rpm", "temperature_celsius"),
    ),
}


class ChihirosDevice:
    """Concrete BLE client for a Chihiros LED device."""

    _logger: logging.Logger

    def __init__(
        self,
        ble_device: BLEDevice,
        model: DeviceModel = FALLBACK,
        advertisement_data: AdvertisementData | None = None,
    ) -> None:
        """Create a device client."""
        self._ble_device = ble_device
        self.model = model
        self._logger = logging.getLogger(ble_device.address.replace(":", "-"))
        self._advertisement_data = advertisement_data
        self._client: BleakClientWithServiceCache | None = None
        self._disconnect_timer: asyncio.TimerHandle | None = None
        self._operation_lock: asyncio.Lock = asyncio.Lock()
        self._read_char: BleakGATTCharacteristic | None = None
        self._write_char: BleakGATTCharacteristic | None = None
        self._connect_lock: asyncio.Lock = asyncio.Lock()
        self._expected_disconnect = False
        self._unexpected_disconnect = asyncio.Event()
        self._msg_id = next_message_id()
        self._fan_auto = False
        self._fan_start_temp = 38
        self._fan_stop_temp = 33
        self._temp_protect = False
        self._bluetooth_led = False
        self._notification_callbacks: set[NotificationCallback] = set()
        self.last_runtime_notification: RuntimeNotification | None = None
        self.last_fan_status_notification: FanStatusNotification | None = None
        self.last_schedule_snapshot_notification: ScheduleSnapshotNotification | None = None
        self.last_dosing_totals_notification: DosingTotalsNotification | None = None
        self.last_dosing_daily_notification: DosingDailyNotification | None = None
        self.last_vivid3_fan_status_notification: Vivid3FanStatusNotification | None = None
        self.loop = asyncio.get_running_loop()

    def set_log_level(self, level: int | str) -> None:
        """Set log level."""
        if isinstance(level, str):
            level = logging._nameToLevel.get(level, logging.INFO)
        self._logger.setLevel(level)

    def set_ble_device_and_advertisement_data(
        self, ble_device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """Update the BLE device and advertisement data."""
        self._ble_device = ble_device
        self._advertisement_data = advertisement_data

    @property
    def current_msg_id(self) -> tuple[int, int]:
        """Get the current message id."""
        return self._msg_id

    def get_next_msg_id(self) -> tuple[int, int]:
        """Get the next message id."""
        self._msg_id = next_message_id(self._msg_id)
        return self._msg_id

    @property
    def model_name(self) -> str:
        """Get the model name."""
        return self.model.name

    @property
    def model_codes(self) -> tuple[str, ...]:
        """Return the model codes."""
        return self.model.advertised_codes

    @property
    def colors(self) -> dict[str, int]:
        """Return supported color channels."""
        return dict(self.model.color_channels)

    @property
    def address(self) -> str:
        """Return the BLE address."""
        return self._ble_device.address

    @property
    def name(self) -> str:
        """Get the device name."""
        if hasattr(self._ble_device, "name"):
            return self._ble_device.name or self._ble_device.address
        return self._ble_device.address

    @property
    def rssi(self) -> int | None:
        """Get the RSSI from the latest advertisement data."""
        if self._advertisement_data:
            return self._advertisement_data.rssi
        return None

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

    def add_notification_callback(self, callback: NotificationCallback) -> Callable[[], None]:
        """Register a callback for parsed device notifications."""
        self._notification_callbacks.add(callback)

        def remove_callback() -> None:
            self._notification_callbacks.discard(callback)

        return remove_callback

    async def query_status(self) -> None:
        """Ask the device to send its runtime/status notification snapshot."""
        cmd = commands.create_query_status_command(self.get_next_msg_id())
        await self._send_command(cmd, 3, notification_wait=STATUS_NOTIFICATION_WAIT)

    async def set_fan_speed(self, speed_percent: int) -> None:
        """Set the fan speed percentage on fan-equipped models.

        Values below the model's minimum are clamped to it (the app clamps the
        VIVID III to 25 %). Manual speed leaves temperature auto mode.
        """
        if not self.model.has_fan:
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
        if not self.model.has_fan:
            raise ValueError(f"Model does not support fan control: {self.model.name}")
        cmd = commands.create_fan_auto_mode_command(self.get_next_msg_id())
        await self._send_command(cmd, 3)
        self._fan_auto = True

    async def set_fan_start_stop_temp(self, start_temp: int, stop_temp: int) -> None:
        """Set the VIVID3 fan start/stop temperatures used by auto mode."""
        if not self.model.has_fan:
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
        if not self.model.is_vivid3:
            raise ValueError(f"Model does not support temperature protection: {self.model.name}")
        cmd = commands.create_vivid3_temp_protect_command(self.get_next_msg_id(), enabled)
        await self._send_command(cmd, 3)
        self._temp_protect = enabled

    async def set_bluetooth_led(self, enabled: bool) -> None:
        """Toggle the VIVID3 indicator LED.

        Matches the vendor app's ``Vivid3Info::setLed()`` frame. The device
        sends no acknowledgement, so the new state is tracked optimistically.
        """
        if not self.model.is_vivid3:
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
            sea_led_family=self.model.sea_led_family,
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
                    sea_led_family=self.model.sea_led_family,
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

    async def _send_command(
        self,
        command: list[bytes] | bytes | bytearray,
        retry: int | None = None,
        notification_wait: float | None = None,
    ) -> None:
        """Send commands to the device.

        ``notification_wait`` defaults to :data:`COMMAND_NOTIFICATION_WAIT` and
        is resolved at call time so callers (and tests) can override it.
        """
        commands_to_send = command if isinstance(command, list) else [bytes(command)]
        attempts = DEFAULT_ATTEMPTS if retry is None else retry
        if attempts < 1:
            raise ValueError("retry must be at least 1")
        self._logger.debug("%s: Sending commands %s", self.name, [item.hex() for item in commands_to_send])
        if self._operation_lock.locked():
            self._logger.debug("%s: Operation already in progress, waiting; RSSI: %s", self.name, self.rssi)
        if notification_wait is None:
            notification_wait = COMMAND_NOTIFICATION_WAIT
        async with self._operation_lock:
            await self._send_command_locked(commands_to_send, attempts, notification_wait)

    async def _send_command_locked(
        self, commands_to_send: list[bytes], attempts: int, notification_wait: float
    ) -> None:
        """Run complete connection transactions, reconnecting for each retry."""
        for attempt in range(1, attempts + 1):
            try:
                await self._ensure_connected()
                await self._execute_command_locked(commands_to_send)
                if notification_wait:
                    await asyncio.sleep(notification_wait)
                return
            except CharacteristicMissingError:
                self._logger.debug("%s: characteristic missing; RSSI: %s", self.name, self.rssi, exc_info=True)
                raise
            except BLEAK_EXCEPTIONS as ex:
                await self._handle_send_failure(ex, attempt, attempts)
            finally:
                await self._execute_disconnect()

    async def _handle_send_failure(self, ex: Exception, attempt: int, attempts: int) -> None:
        """Log a failed communication attempt and retry or give up."""
        if isinstance(ex, BleakNotFoundError):
            self._logger.error("%s: device not found or poor RSSI: %s", self.name, self.rssi, exc_info=True)
        self._logger.debug(
            "%s: communication attempt %s/%s failed: %s", self.name, attempt, attempts, ex, exc_info=True
        )
        if attempt == attempts:
            raise
        if isinstance(ex, BleakDBusError):
            await asyncio.sleep(BLEAK_BACKOFF_TIME)

    def _require_write_characteristics(self) -> BleakGATTCharacteristic:
        """Return the write characteristic after checking both UART characteristics exist."""
        if not self._read_char:
            raise CharacteristicMissingError("Read characteristic missing")
        if not self._write_char:
            raise CharacteristicMissingError("Write characteristic missing")
        return self._write_char

    async def _execute_command_locked(self, commands_to_send: list[bytes]) -> None:
        """Write commands to the BLE characteristic."""
        assert self._client is not None  # nosec
        write_char = self._require_write_characteristics()
        for index, command in enumerate(commands_to_send):
            await self._client.write_gatt_char(write_char, command, False)
            if self._unexpected_disconnect.is_set():
                raise BleakError("Device unexpectedly disconnected during command batch")
            if index < len(commands_to_send) - 1:
                await asyncio.sleep(BATCH_WRITE_DELAY)

    def _notification_handler(self, _sender: BleakGATTCharacteristic, data: bytearray) -> None:
        """Handle notification responses."""
        parsed = parse_notification(data, self.model.color_channels)
        if parsed is None:
            self._logger.debug("%s: Notification received: %s", self.name, data.hex())
            return
        if isinstance(parsed, Vivid3FanStatusNotification) and not self.model.has_fan:
            # 0xB6/0x16 frames are the VIVID3 fan readout; ignore on non-fan models.
            self._logger.debug("%s: Ignoring fan readout frame on non-fan model %s", self.name, self.model.name)
            return
        self._record_notification(parsed)
        self._notify_callbacks(parsed)

    def _record_notification(self, parsed: ParsedNotification) -> None:
        """Store a parsed notification on its last-seen attribute and log it."""
        attribute, message, field_names = _LAST_NOTIFICATION_FIELDS[type(parsed)]
        setattr(self, attribute, parsed)
        self._logger.debug(
            "%s: %s",
            self.name,
            message % tuple(getattr(parsed, field_name) for field_name in field_names),
        )

    def _notify_callbacks(self, notification: ParsedNotification) -> None:
        """Notify subscribers about a parsed device notification."""
        for callback in tuple(self._notification_callbacks):
            try:
                callback(notification)
            except Exception:
                self._logger.exception("Notification callback failed for %s", self.name)

    def _disconnected(self, client: BleakClientWithServiceCache) -> None:
        """Handle disconnected callback."""
        if self._expected_disconnect:
            self._logger.debug("%s: Disconnected from device; RSSI: %s", self.name, self.rssi)
            return
        self._logger.warning(
            "%s: Device unexpectedly disconnected; RSSI: %s",
            self.name,
            self.rssi,
        )
        self._unexpected_disconnect.set()

    def _resolve_characteristics(self, services: BleakGATTServiceCollection) -> bool:
        """Resolve UART characteristics.

        Tries Nordic UART first (SeaLed / NewBleLed / VIVID III), then falls back
        to HM-10 (BleLed devices).
        """
        self._read_char = services.get_characteristic(UART_TX_CHAR_UUID)
        self._write_char = services.get_characteristic(UART_RX_CHAR_UUID)
        if not (self._read_char and self._write_char):
            self._read_char = services.get_characteristic(HM10_TX_CHAR_UUID)
            self._write_char = services.get_characteristic(HM10_RX_CHAR_UUID)
        return bool(self._read_char and self._write_char)

    def _is_connected(self) -> bool:
        """Return whether an established BLE client exists."""
        return bool(self._client and self._client.is_connected)

    async def _ensure_connected(self) -> None:
        """Ensure a BLE connection exists."""
        if self._connect_lock.locked():
            self._logger.debug(
                "%s: Connection already in progress, waiting; RSSI: %s",
                self.name,
                self.rssi,
            )
        if self._is_connected():
            self._reset_disconnect_timer()
            return
        async with self._connect_lock:
            if self._is_connected():
                self._reset_disconnect_timer()
                return
            await self._establish_connection()

    async def _establish_connection(self) -> None:
        """Establish the BLE connection and configure it, cleaning up on failure."""
        self._logger.debug("%s: Connecting; RSSI: %s", self.name, self.rssi)
        self._unexpected_disconnect.clear()
        client = await establish_connection(
            BleakClientWithServiceCache,
            self._ble_device,
            self.name,
            self._disconnected,
            use_services_cache=True,
            ble_device_callback=lambda: self._ble_device,
        )
        self._logger.debug("%s: Connected; RSSI: %s", self.name, self.rssi)
        try:
            await self._configure_client(client)
        except Exception:
            await self._abort_connection(client)
            raise

    async def _configure_client(self, client: BleakClientWithServiceCache) -> None:
        """Resolve characteristics, subscribe to notifications, and run the prelude."""
        resolved = self._resolve_characteristics(client.services)
        if not resolved:
            resolved = self._resolve_characteristics(await client.get_services())
        if not resolved:
            raise CharacteristicMissingError("UART characteristics missing")

        self._client = client
        self._reset_disconnect_timer()

        self._logger.debug("%s: Subscribe to notifications; RSSI: %s", self.name, self.rssi)
        await client.start_notify(self._read_char, self._notification_handler)  # type: ignore
        await self._send_connection_prelude(client)

    async def _abort_connection(self, client: BleakClientWithServiceCache) -> None:
        """Tear down partial connection state after a failed setup."""
        read_char = self._read_char
        self._client = None
        self._read_char = None
        self._write_char = None
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
            self._disconnect_timer = None
        self._expected_disconnect = True
        await self._disconnect_client(client, read_char)

    async def _send_connection_prelude(self, client: BleakClientWithServiceCache) -> None:
        """Send the LED startup sequence observed in the vendor app/ESPHome flow."""
        if not self._write_char:
            raise CharacteristicMissingError("Write characteristic missing")
        prelude = [
            commands.create_base_auth_command(self.get_next_msg_id()),
            commands.create_set_time_command(self.get_next_msg_id()),
            commands.create_set_time_command(self.get_next_msg_id()),
        ]
        self._logger.debug(
            "%s: Sending connection prelude %s",
            self.name,
            [command.hex() for command in prelude],
        )
        for index, command in enumerate(prelude):
            await client.write_gatt_char(self._write_char, command, False)
            if index < len(prelude) - 1:
                await asyncio.sleep(BATCH_WRITE_DELAY)

    def _reset_disconnect_timer(self) -> None:
        """Reset connection state without scheduling a delayed keepalive."""
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
            self._disconnect_timer = None
        self._expected_disconnect = False

    async def disconnect(self) -> None:
        """Disconnect from the device."""
        self._logger.debug("%s: Disconnecting", self.name)
        await self._execute_disconnect()

    async def _execute_disconnect(self) -> None:
        """Execute disconnection."""
        async with self._connect_lock:
            read_char = self._read_char
            client = self._client
            self._expected_disconnect = True
            if self._disconnect_timer:
                self._disconnect_timer.cancel()
                self._disconnect_timer = None
            self._client = None
            self._read_char = None
            self._write_char = None
            if client:
                await self._disconnect_client(client, read_char)

    async def _disconnect_client(
        self,
        client: BleakClientWithServiceCache,
        read_char: BleakGATTCharacteristic | None,
    ) -> None:
        """Disconnect an established BLE client without taking the connection lock."""
        if not client.is_connected:
            return
        if read_char:
            try:
                await client.stop_notify(read_char)
            except BleakError:
                self._logger.debug("%s: Failed to stop notifications", self.name, exc_info=True)
        try:
            await client.disconnect()
        except BleakError:
            self._logger.debug("%s: Failed to disconnect", self.name, exc_info=True)


class ChihirosDosingPump(ChihirosDevice):
    """Concrete BLE client for a Chihiros dosing pump."""

    async def dose_ml(self, pump_idx: int, volume_ml: float) -> None:
        """Trigger an immediate manual dose on one pump channel."""
        commands_to_send = [
            commands.create_dose_auth_1_command(self.get_next_msg_id()),
            commands.create_dose_auth_2_command(self.get_next_msg_id()),
            commands.create_manual_dose_command(self.get_next_msg_id(), pump_idx, volume_ml),
        ]
        # A disconnect after the final write is ambiguous: the pump may already
        # have accepted the dose. Never replay this non-idempotent transaction.
        await self._send_command(commands_to_send, retry=1)
