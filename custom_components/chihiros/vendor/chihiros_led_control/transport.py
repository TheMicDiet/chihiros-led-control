"""BLE transport boundary shared by family device drivers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from inspect import isawaitable
from typing import Protocol

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.backends.service import BleakGATTCharacteristic, BleakGATTServiceCollection
from bleak.exc import BleakDBusError
from bleak_retry_connector import BLEAK_RETRY_EXCEPTIONS as BLEAK_EXCEPTIONS
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakError,
    BleakNotFoundError,
    establish_connection,
)

from .const import CUSTOM_NOTIFY_CHAR_UUID, HM10_RX_CHAR_UUID, UART_RX_CHAR_UUID, UART_TX_CHAR_UUID
from .exceptions import CharacteristicMissingError

BLEAK_BACKOFF_TIME = 0.25
DISCONNECT_DELAY = 120
BATCH_WRITE_DELAY = 0.03

RawNotificationCallback = Callable[[object, bytearray], None]
PreludeResult = Sequence[bytes] | Awaitable[Sequence[bytes]]
PreludeCallback = Callable[[], PreludeResult]


class ChihirosTransport(Protocol):
    """Transport contract consumed by device drivers.

    Drivers provide raw notification and lazy-prelude callbacks once they have
    selected a profile.  The transport knows only how to establish a session,
    write encoded frame batches, and deliver bytes back to the driver.
    """

    async def send(
        self,
        frames: Sequence[bytes],
        *,
        attempts: int,
        notification_wait: float,
    ) -> None:
        """Send an already encoded frame batch."""

    async def disconnect(self) -> None:
        """Disconnect the current transport session."""

    def update_device(self, ble_device: BLEDevice, advertisement_data: AdvertisementData | None) -> None:
        """Update the transport's current BLE identity."""

    def set_callbacks(
        self,
        *,
        notification_callback: RawNotificationCallback,
        prelude_callback: PreludeCallback,
    ) -> None:
        """Set raw notification and lazy connection-prelude callbacks."""


def pair_notify_characteristic(
    services: BleakGATTServiceCollection, write_char: BleakGATTCharacteristic | None
) -> BleakGATTCharacteristic | None:
    """Return the notify characteristic paired with the resolved write endpoint."""
    if write_char is not None and write_char.uuid.lower() == UART_RX_CHAR_UUID.lower():
        return services.get_characteristic(UART_TX_CHAR_UUID)
    return services.get_characteristic(CUSTOM_NOTIFY_CHAR_UUID) or services.get_characteristic(UART_TX_CHAR_UUID)


class BleTransport:
    """Production BLE transport owning connection and write lifecycle."""

    def __init__(
        self,
        ble_device: BLEDevice,
        advertisement_data: AdvertisementData | None = None,
        *,
        notification_callback: RawNotificationCallback | None = None,
        prelude_callback: PreludeCallback | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Create a transport for one BLE identity."""
        self.ble_device = ble_device
        self.advertisement_data = advertisement_data
        self._logger = logger or logging.getLogger(self._log_name())
        self._notification_callback = notification_callback
        self._prelude_callback = prelude_callback
        self._client: BleakClientWithServiceCache | None = None
        self._read_char: BleakGATTCharacteristic | None = None
        self._write_char: BleakGATTCharacteristic | None = None
        self._connect_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._unexpected_disconnect = asyncio.Event()
        self._expected_disconnect = False
        self._disconnect_timer: asyncio.TimerHandle | None = None
        self._disconnect_timer_generation = 0
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def _log_name(self) -> str:
        """Return a stable logger name for the current BLE identity."""
        address = getattr(self.ble_device, "address", "unknown")
        return str(address).replace(":", "-")

    @property
    def name(self) -> str:
        """Return the latest advertised device name or address."""
        name = getattr(self.ble_device, "name", None)
        return name or getattr(self.ble_device, "address", "unknown")

    @property
    def rssi(self) -> int | None:
        """Return the latest advertisement RSSI."""
        return self.advertisement_data.rssi if self.advertisement_data is not None else None

    @property
    def is_connected(self) -> bool:
        """Return whether an active BLE client exists."""
        return bool(self._client and self._client.is_connected)

    @property
    def batch_write_delay(self) -> float:
        """Return the pacing interval between frames in one batch."""
        return BATCH_WRITE_DELAY

    def set_callbacks(
        self,
        *,
        notification_callback: RawNotificationCallback,
        prelude_callback: PreludeCallback,
    ) -> None:
        """Set callbacks without making the transport depend on a driver."""
        self._notification_callback = notification_callback
        self._prelude_callback = prelude_callback

    def update_device(self, ble_device: BLEDevice, advertisement_data: AdvertisementData | None) -> None:
        """Update the BLE identity used by future connection attempts."""
        self.ble_device = ble_device
        self.advertisement_data = advertisement_data

    async def send(self, frames: Sequence[bytes], *, attempts: int, notification_wait: float) -> None:
        """Connect lazily, run the prelude once per connection, and write a batch."""
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        commands_to_send = [bytes(frame) for frame in frames]
        if not commands_to_send:
            return
        if self._operation_lock.locked():
            self._logger.debug("%s: Operation already in progress, waiting; RSSI: %s", self.name, self.rssi)
        async with self._operation_lock:
            await self._send_attempts(commands_to_send, attempts, notification_wait)

    async def _send_attempts(self, frames: Sequence[bytes], attempts: int, notification_wait: float) -> None:
        """Run transaction attempts while retaining the operation lock."""
        for attempt in range(1, attempts + 1):
            if await self._send_attempt(frames, attempt, attempts, notification_wait):
                return

    async def _send_attempt(
        self,
        frames: Sequence[bytes],
        attempt: int,
        attempts: int,
        notification_wait: float,
    ) -> bool:
        """Run one transaction attempt and report whether it succeeded."""
        try:
            await self._send_transaction(frames, notification_wait)
        except (CharacteristicMissingError, asyncio.CancelledError):
            await self._execute_disconnect()
            raise
        except BLEAK_EXCEPTIONS as ex:
            await self._execute_disconnect()
            self._handle_send_failure(ex, attempt, attempts)
            await self._wait_before_retry(ex, attempt, attempts)
            return False
        self._schedule_disconnect_timer()
        return True

    async def _wait_before_retry(self, ex: Exception, attempt: int, attempts: int) -> None:
        """Apply the vendor backoff only for retryable DBus failures."""
        if attempt >= attempts:
            return
        if isinstance(ex, BleakDBusError):
            await asyncio.sleep(BLEAK_BACKOFF_TIME)

    async def _send_transaction(self, frames: Sequence[bytes], notification_wait: float) -> None:
        """Write one complete frame batch over the current connection."""
        await self._ensure_connected()
        await self._write_frames(frames)
        if notification_wait:
            await asyncio.sleep(notification_wait)

    def _handle_send_failure(self, ex: Exception, attempt: int, attempts: int) -> None:
        """Log one failed communication attempt and re-raise on exhaustion."""
        if isinstance(ex, BleakNotFoundError):
            self._logger.error("%s: device not found or poor RSSI: %s", self.name, self.rssi, exc_info=True)
        self._logger.debug(
            "%s: communication attempt %s/%s failed: %s", self.name, attempt, attempts, ex, exc_info=True
        )
        if attempt == attempts:
            raise ex

    async def _ensure_connected(self) -> None:
        """Ensure a configured BLE connection exists."""
        self._cancel_disconnect_timer()
        if self.is_connected:
            return
        if self._connect_lock.locked():
            self._logger.debug("%s: Connection already in progress, waiting; RSSI: %s", self.name, self.rssi)
        async with self._connect_lock:
            if self.is_connected:
                return
            await self._establish_connection()

    async def _establish_connection(self) -> None:
        """Establish and configure one BLE session."""
        self._logger.debug("%s: Connecting; RSSI: %s", self.name, self.rssi)
        self._expected_disconnect = False
        self._unexpected_disconnect.clear()
        client = await self._establish_ble_client()
        self._logger.debug("%s: Connected; RSSI: %s", self.name, self.rssi)
        try:
            await self._configure_client(client)
        except (Exception, asyncio.CancelledError):
            await self._abort_connection(client)
            raise

    async def _establish_ble_client(self) -> BleakClientWithServiceCache:
        """Open one BLE connection using the retry connector."""
        return await establish_connection(
            BleakClientWithServiceCache,
            self.ble_device,
            self.name,
            self._disconnected,
            use_services_cache=True,
            ble_device_callback=lambda: self.ble_device,
        )

    async def _configure_client(self, client: BleakClientWithServiceCache) -> None:
        """Resolve endpoints, subscribe to notifications, and lazily send prelude."""
        if not await self._resolve_client_characteristics(client):
            raise CharacteristicMissingError("Write characteristic missing")
        self._client = client
        await self._subscribe_client(client)
        await self._send_prelude()

    async def _resolve_client_characteristics(self, client: BleakClientWithServiceCache) -> bool:
        """Resolve endpoints from cached services, then the live service list."""
        resolved = self._resolve_characteristics(client.services)
        if not resolved:
            resolved = self._resolve_characteristics(await client.get_services())
        return resolved

    async def _subscribe_client(self, client: BleakClientWithServiceCache) -> None:
        """Subscribe to notifications when a paired endpoint is available."""
        if self._read_char is not None:
            self._logger.debug("%s: Subscribe to notifications; RSSI: %s", self.name, self.rssi)
            callback = self._notification_handler
            await client.start_notify(self._read_char, callback)
        else:
            self._logger.warning(
                "%s: No notify characteristic (8ec90003/6e400003) found; "
                "running fire-and-forget without state notifications; RSSI: %s",
                self.name,
                self.rssi,
            )

    async def _send_prelude(self) -> None:
        """Run the connection prelude, if configured."""
        if self._prelude_callback is None:
            return
        prelude = self._prelude_callback()
        if isawaitable(prelude):
            prelude = await prelude
        await self._write_frames(prelude)

    async def _abort_connection(self, client: BleakClientWithServiceCache) -> None:
        """Tear down partial connection state after failed setup."""
        read_char = self._read_char
        self._client = None
        self._read_char = None
        self._write_char = None
        self._cancel_disconnect_timer()
        await self._disconnect_client(client, read_char)

    def _resolve_characteristics(self, services: BleakGATTServiceCollection) -> bool:
        """Resolve write and paired notify characteristics by UUID."""
        self._write_char = services.get_characteristic(HM10_RX_CHAR_UUID) or services.get_characteristic(
            UART_RX_CHAR_UUID
        )
        self._read_char = pair_notify_characteristic(services, self._write_char)
        return self._write_char is not None

    def _notification_handler(self, sender: object, data: bytearray) -> None:
        """Forward raw notifications to the owning device driver."""
        if self._notification_callback is None:
            return
        try:
            self._notification_callback(sender, data)
        except Exception:
            self._logger.exception("Raw notification callback failed for %s", self.name)

    def _disconnected(self, client: BleakClientWithServiceCache) -> None:
        """Handle expected and unexpected physical disconnects."""
        if client is not self._client:
            self._logger.debug("%s: Disconnected from device; RSSI: %s", self.name, self.rssi)
            return
        self._cancel_disconnect_timer()
        self._client = None
        self._read_char = None
        self._write_char = None
        if self._expected_disconnect:
            self._logger.debug("%s: Disconnected from device; RSSI: %s", self.name, self.rssi)
            return
        self._logger.warning("%s: Device unexpectedly disconnected; RSSI: %s", self.name, self.rssi)
        self._unexpected_disconnect.set()

    async def _write_frames(self, frames: Sequence[bytes]) -> None:
        """Write an encoded frame batch with vendor pacing."""
        client = self._client
        if client is None:
            raise BleakError("Device is not connected")
        write_char = self._write_char
        if write_char is None:
            raise CharacteristicMissingError("Write characteristic missing")
        for index, frame in enumerate(frames):
            await self._write_frame(client, write_char, frame)
            if index < len(frames) - 1:
                await asyncio.sleep(self.batch_write_delay)

    async def _write_frame(
        self,
        client: BleakClientWithServiceCache,
        write_char: BleakGATTCharacteristic,
        frame: bytes,
    ) -> None:
        """Write one frame while checking for a physical disconnect."""
        if self._unexpected_disconnect.is_set():
            raise BleakError("Device unexpectedly disconnected during command batch")
        await client.write_gatt_char(write_char, frame, False)
        if self._unexpected_disconnect.is_set():
            raise BleakError("Device unexpectedly disconnected during command batch")

    def _cancel_disconnect_timer(self) -> None:
        """Cancel an idle disconnect and invalidate its callback."""
        self._disconnect_timer_generation += 1
        if self._disconnect_timer is not None:
            self._disconnect_timer.cancel()
            self._disconnect_timer = None

    def _schedule_disconnect_timer(self) -> None:
        """Disconnect the current client after the idle timeout."""
        self._cancel_disconnect_timer()
        client = self._client
        if client is None or not client.is_connected:
            return
        generation = self._disconnect_timer_generation
        loop = self._loop or asyncio.get_running_loop()
        self._loop = loop
        self._disconnect_timer = loop.call_later(
            DISCONNECT_DELAY,
            self._disconnect_after_timeout,
            generation,
            client,
        )

    def _disconnect_after_timeout(self, generation: int, client: BleakClientWithServiceCache) -> None:
        """Start idle disconnect work from the event-loop timer callback."""
        if generation != self._disconnect_timer_generation:
            return
        self._disconnect_timer = None
        self._loop.create_task(self._execute_timed_disconnect(generation, client))

    async def _execute_timed_disconnect(self, generation: int, client: BleakClientWithServiceCache) -> None:
        """Disconnect only when no newer operation refreshed the deadline."""
        async with self._operation_lock:
            if generation != self._disconnect_timer_generation or client is not self._client:
                return
            self._logger.debug("%s: Disconnecting after timeout of %s seconds", self.name, DISCONNECT_DELAY)
            await self._execute_disconnect()

    async def disconnect(self) -> None:
        """Disconnect from the device."""
        self._logger.debug("%s: Disconnecting", self.name)
        async with self._operation_lock:
            await self._execute_disconnect()

    async def _execute_disconnect(self) -> None:
        """Disconnect the active session while holding the connection lock."""
        async with self._connect_lock:
            read_char = self._read_char
            client = self._client
            self._cancel_disconnect_timer()
            self._expected_disconnect = True
            self._client = None
            self._read_char = None
            self._write_char = None
            if client is not None:
                await self._disconnect_client(client, read_char)

    async def _disconnect_client(
        self,
        client: BleakClientWithServiceCache,
        read_char: BleakGATTCharacteristic | None,
    ) -> None:
        """Disconnect one BLE client without taking the connection lock."""
        if not client.is_connected:
            return
        if read_char is not None:
            try:
                await client.stop_notify(read_char)
            except BleakError:
                self._logger.debug("%s: Failed to stop notifications", self.name, exc_info=True)
        try:
            await client.disconnect()
        except BleakError:
            self._logger.debug("%s: Failed to disconnect", self.name, exc_info=True)


__all__ = [
    "BATCH_WRITE_DELAY",
    "BleTransport",
    "ChihirosTransport",
    "PreludeCallback",
    "RawNotificationCallback",
    "pair_notify_characteristic",
    "PreludeResult",
]
