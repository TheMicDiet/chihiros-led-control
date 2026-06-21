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
    retry_bluetooth_connection_error,
)

from . import commands
from .const import UART_RX_CHAR_UUID, UART_TX_CHAR_UUID
from .exceptions import CharacteristicMissingError
from .models import FALLBACK, DeviceModel
from .protocol import (
    ParsedNotification,
    RuntimeNotification,
    ScheduleSnapshotNotification,
    next_message_id,
    parse_notification,
)
from .weekday_encoding import WeekdaySelect, encode_selected_weekdays

DEFAULT_ATTEMPTS = 3
BLEAK_BACKOFF_TIME = 0.25
COMMAND_NOTIFICATION_WAIT = 0.5
STATUS_NOTIFICATION_WAIT = 1.0
NotificationCallback = Callable[[ParsedNotification], None]


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
        self._msg_id = next_message_id()
        self._notification_callbacks: set[NotificationCallback] = set()
        self.last_runtime_notification: RuntimeNotification | None = None
        self.last_schedule_snapshot_notification: ScheduleSnapshotNotification | None = None
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

    async def _set_channel_brightness(
        self,
        brightness: int,
        color: str | int,
    ) -> None:
        """Set brightness of one color channel."""
        color_id = self._color_id(color)
        if color_id is None:
            self._logger.warning("Color not supported: `%s`", color)
            return
        cmd = commands.create_set_brightness_command(self.get_next_msg_id(), color_id, brightness)
        await self._send_command(cmd, 3)

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
            self._validate_brightness_levels(tuple(brightness.values()))
            result: dict[int, int] = {}
            for color, level in brightness.items():
                color_id = self._color_id(color)
                if color_id is None:
                    raise ValueError(f"Color not supported: {color}")
                result[color_id] = level
            return result

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

    def _channel_count(self) -> int:
        """Return number of protocol channel slots for this model."""
        return max(self.model.color_channels.values()) + 1

    def _brightness_parameter_values(self, brightness: int | Sequence[int] | Mapping[str | int, int]) -> list[int]:
        """Return auto schedule brightness parameters ordered by channel id."""
        brightness_by_channel = self._normalize_brightness(brightness)
        return [brightness_by_channel.get(channel_id, 255) for channel_id in range(self._channel_count())]

    async def set_brightness(self, brightness: int | Sequence[int] | Mapping[str | int, int]) -> None:
        """Set light brightness."""
        for color_id, brightness_level in self._normalize_brightness(brightness).items():
            await self._set_channel_brightness(brightness_level, color_id)

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

    async def enable_auto_mode(self, timestamp: datetime | None = None) -> None:
        """Enable auto mode."""
        time_cmd = commands.create_set_time_command(self.get_next_msg_id(), timestamp)
        switch_cmd = commands.create_switch_to_auto_mode_command(self.get_next_msg_id())
        await self._send_command(time_cmd, 3)
        await self._send_command(switch_cmd, 3)

    async def set_manual_mode(self) -> None:
        """Switch to manual mode."""
        await self.turn_on()

    async def _send_command(
        self,
        command: list[bytes] | bytes | bytearray,
        retry: int | None = None,
        notification_wait: float = COMMAND_NOTIFICATION_WAIT,
    ) -> None:
        """Send commands to the device."""
        try:
            await self._ensure_connected()
            commands_to_send: list[bytes]
            if isinstance(command, list):
                commands_to_send = command
            else:
                commands_to_send = [bytes(command)]
            await self._send_command_while_connected(commands_to_send, retry)
            if notification_wait:
                await asyncio.sleep(notification_wait)
        finally:
            await self._execute_disconnect()

    async def _send_command_while_connected(self, commands_to_send: list[bytes], retry: int | None = None) -> None:
        """Send commands while connected."""
        self._logger.debug(
            "%s: Sending commands %s",
            self.name,
            [command.hex() for command in commands_to_send],
        )
        if self._operation_lock.locked():
            self._logger.debug(
                "%s: Operation already in progress, waiting; RSSI: %s",
                self.name,
                self.rssi,
            )
        async with self._operation_lock:
            try:
                await self._send_command_locked(commands_to_send)
                return
            except BleakNotFoundError:
                self._logger.error(
                    "%s: device not found, no longer in range, or poor RSSI: %s",
                    self.name,
                    self.rssi,
                    exc_info=True,
                )
                raise
            except CharacteristicMissingError as ex:
                self._logger.debug(
                    "%s: characteristic missing: %s; RSSI: %s",
                    self.name,
                    ex,
                    self.rssi,
                    exc_info=True,
                )
                raise
            except BLEAK_EXCEPTIONS:
                self._logger.debug("%s: communication failed", self.name, exc_info=True)
                raise

        raise RuntimeError("Unreachable")

    @retry_bluetooth_connection_error(DEFAULT_ATTEMPTS)
    async def _send_command_locked(self, commands_to_send: list[bytes]) -> None:
        """Send commands and retry transient Bluetooth failures."""
        try:
            await self._execute_command_locked(commands_to_send)
        except BleakDBusError as ex:
            await asyncio.sleep(BLEAK_BACKOFF_TIME)
            self._logger.debug(
                "%s: RSSI: %s; backing off %ss; disconnecting due to error: %s",
                self.name,
                self.rssi,
                BLEAK_BACKOFF_TIME,
                ex,
            )
            await self._execute_disconnect()
            raise
        except BleakError as ex:
            self._logger.debug("%s: RSSI: %s; disconnecting due to error: %s", self.name, self.rssi, ex)
            await self._execute_disconnect()
            raise

    async def _execute_command_locked(self, commands_to_send: list[bytes]) -> None:
        """Write commands to the BLE characteristic."""
        assert self._client is not None  # nosec
        if not self._read_char:
            raise CharacteristicMissingError("Read characteristic missing")
        if not self._write_char:
            raise CharacteristicMissingError("Write characteristic missing")
        for command in commands_to_send:
            await self._client.write_gatt_char(self._write_char, command, False)

    def _notification_handler(self, _sender: BleakGATTCharacteristic, data: bytearray) -> None:
        """Handle notification responses."""
        parsed = parse_notification(data, self.model.color_channels)
        if isinstance(parsed, RuntimeNotification):
            self.last_runtime_notification = parsed
            self._logger.debug(
                "%s: Runtime notification received; firmware=%s runtime_minutes=%s",
                self.name,
                parsed.firmware_version,
                parsed.runtime_minutes,
            )
            self._notify_callbacks(parsed)
            return
        if isinstance(parsed, ScheduleSnapshotNotification):
            self.last_schedule_snapshot_notification = parsed
            self._logger.debug(
                "%s: Schedule snapshot notification received; firmware=%s points=%s",
                self.name,
                parsed.firmware_version,
                parsed.points,
            )
            self._notify_callbacks(parsed)
            return
        self._logger.debug("%s: Notification received: %s", self.name, data.hex())

    def _notify_callbacks(self, notification: ParsedNotification) -> None:
        """Notify subscribers about a parsed device notification."""
        for callback in tuple(self._notification_callbacks):
            callback(notification)

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

    def _resolve_characteristics(self, services: BleakGATTServiceCollection) -> bool:
        """Resolve UART characteristics."""
        for characteristic in [UART_TX_CHAR_UUID]:
            if char := services.get_characteristic(characteristic):
                self._read_char = char
                break
        for characteristic in [UART_RX_CHAR_UUID]:
            if char := services.get_characteristic(characteristic):
                self._write_char = char
                break
        return bool(self._read_char and self._write_char)

    async def _ensure_connected(self) -> None:
        """Ensure a BLE connection exists."""
        if self._connect_lock.locked():
            self._logger.debug(
                "%s: Connection already in progress, waiting; RSSI: %s",
                self.name,
                self.rssi,
            )
        if self._client and self._client.is_connected:
            self._reset_disconnect_timer()
            return
        async with self._connect_lock:
            if self._client and self._client.is_connected:
                self._reset_disconnect_timer()
                return
            self._logger.debug("%s: Connecting; RSSI: %s", self.name, self.rssi)
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
            except Exception:
                read_char = self._read_char
                self._client = None
                self._read_char = None
                self._write_char = None
                if self._disconnect_timer:
                    self._disconnect_timer.cancel()
                    self._disconnect_timer = None
                self._expected_disconnect = True
                await self._disconnect_client(client, read_char)
                raise

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
        for command in prelude:
            await client.write_gatt_char(self._write_char, command, False)

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
        await client.disconnect()


class ChihirosDosingPump(ChihirosDevice):
    """Concrete BLE client for a Chihiros dosing pump."""

    async def dose_ml(self, pump_idx: int, volume_ml: float) -> None:
        """Trigger an immediate manual dose on one pump channel."""
        commands_to_send = [
            commands.create_dose_auth_1_command(self.get_next_msg_id()),
            commands.create_dose_auth_2_command(self.get_next_msg_id()),
            commands.create_manual_dose_command(self.get_next_msg_id(), pump_idx, volume_ml),
        ]
        await self._send_command(commands_to_send, 3)
