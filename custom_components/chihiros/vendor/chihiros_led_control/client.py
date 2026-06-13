"""Runtime BLE client for Chihiros LED devices."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.backends.service import BleakGATTCharacteristic  # type: ignore
from bleak.backends.service import BleakGATTServiceCollection
from bleak.exc import BleakDBusError
from bleak_retry_connector import BLEAK_RETRY_EXCEPTIONS as BLEAK_EXCEPTIONS
from bleak_retry_connector import BleakError  # type: ignore
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakNotFoundError,
    establish_connection,
    retry_bluetooth_connection_error,
)

from . import commands
from .const import UART_RX_CHAR_UUID, UART_TX_CHAR_UUID
from .exceptions import CharacteristicMissingError
from .models import FALLBACK, DeviceModel
from .protocol import next_message_id
from .weekday_encoding import WeekdaySelect, encode_selected_weekdays

DEFAULT_ATTEMPTS = 3
DISCONNECT_DELAY = 120
BLEAK_BACKOFF_TIME = 0.25


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

    async def set_color_brightness(
        self,
        brightness: int,
        color: str | int = 0,
    ) -> None:
        """Set brightness of a color channel."""
        color_id: int | None = None
        colors = self.model.color_channels
        if isinstance(color, int) and color in colors.values():
            color_id = color
        elif isinstance(color, str) and color in colors:
            color_id = colors[color]
        if color_id is None:
            self._logger.warning("Color not supported: `%s`", color)
            return
        cmd = commands.create_manual_setting_command(
            self.get_next_msg_id(), color_id, brightness
        )
        await self._send_command(cmd, 3)

    async def set_brightness(self, brightness: int) -> None:
        """Set light brightness."""
        await self.set_color_brightness(brightness)

    async def set_rgb_brightness(self, brightness: tuple[int, int, int]) -> None:
        """Set RGB brightness."""
        for color_id, brightness_level in enumerate(brightness):
            await self.set_color_brightness(brightness_level, color_id)

    async def turn_on(self) -> None:
        """Turn on the light."""
        for color_name in self.model.color_channels:
            await self.set_color_brightness(100, color_name)

    async def turn_off(self) -> None:
        """Turn off the light."""
        for color_name in self.model.color_channels:
            await self.set_color_brightness(0, color_name)

    async def add_setting(
        self,
        sunrise: datetime,
        sunset: datetime,
        max_brightness: int = 100,
        ramp_up_in_minutes: int = 0,
        weekdays: list[WeekdaySelect] | None = None,
    ) -> None:
        """Add an automation setting to the light."""
        if weekdays is None:
            weekdays = [WeekdaySelect.everyday]
        cmd = commands.create_add_auto_setting_command(
            self.get_next_msg_id(),
            sunrise.time(),
            sunset.time(),
            (max_brightness, 255, 255),
            ramp_up_in_minutes,
            encode_selected_weekdays(weekdays),
        )
        await self._send_command(cmd, 3)

    async def add_rgb_setting(
        self,
        sunrise: datetime,
        sunset: datetime,
        max_brightness: tuple[int, int, int] = (100, 100, 100),
        ramp_up_in_minutes: int = 0,
        weekdays: list[WeekdaySelect] | None = None,
    ) -> None:
        """Add an automation setting to the RGB light."""
        if weekdays is None:
            weekdays = [WeekdaySelect.everyday]
        cmd = commands.create_add_auto_setting_command(
            self.get_next_msg_id(),
            sunrise.time(),
            sunset.time(),
            max_brightness,
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
        )
        await self._send_command(cmd, 3)

    async def reset_settings(self) -> None:
        """Remove all automation settings from the light."""
        cmd = commands.create_reset_auto_settings_command(self.get_next_msg_id())
        await self._send_command(cmd, 3)

    async def enable_auto_mode(self) -> None:
        """Enable auto mode."""
        switch_cmd = commands.create_switch_to_auto_mode_command(self.get_next_msg_id())
        time_cmd = commands.create_set_time_command(self.get_next_msg_id())
        await self._send_command(switch_cmd, 3)
        await self._send_command(time_cmd, 3)

    async def set_manual_mode(self) -> None:
        """Switch to manual mode."""
        for color_name in self.model.color_channels:
            await self.set_color_brightness(100, color_name)

    async def _send_command(
        self, command: list[bytes] | bytes | bytearray, retry: int | None = None
    ) -> None:
        """Send commands to the device."""
        await self._ensure_connected()
        commands_to_send: list[bytes]
        if isinstance(command, list):
            commands_to_send = command
        else:
            commands_to_send = [bytes(command)]
        await self._send_command_while_connected(commands_to_send, retry)

    async def _send_command_while_connected(
        self, commands_to_send: list[bytes], retry: int | None = None
    ) -> None:
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
            self._logger.debug(
                "%s: RSSI: %s; disconnecting due to error: %s", self.name, self.rssi, ex
            )
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

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle notification responses."""
        self._logger.warning("%s: Notification received: %s", self.name, data)

    def _disconnected(self, client: BleakClientWithServiceCache) -> None:
        """Handle disconnected callback."""
        if self._expected_disconnect:
            self._logger.debug(
                "%s: Disconnected from device; RSSI: %s", self.name, self.rssi
            )
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

                self._logger.debug(
                    "%s: Subscribe to notifications; RSSI: %s", self.name, self.rssi
                )
                await client.start_notify(self._read_char, self._notification_handler)  # type: ignore
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

    def _reset_disconnect_timer(self) -> None:
        """Reset the delayed disconnect timer."""
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
        self._expected_disconnect = False
        self._disconnect_timer = self.loop.call_later(
            DISCONNECT_DELAY, self._disconnect
        )

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
                self._logger.debug(
                    "%s: Failed to stop notifications", self.name, exc_info=True
                )
        await client.disconnect()

    def _disconnect(self) -> None:
        """Schedule the timed disconnection."""
        self._disconnect_timer = None
        asyncio.create_task(self._execute_timed_disconnect())

    async def _execute_timed_disconnect(self) -> None:
        """Execute timed disconnection."""
        self._logger.debug(
            "%s: Disconnecting after timeout of %s",
            self.name,
            DISCONNECT_DELAY,
        )
        await self._execute_disconnect()
