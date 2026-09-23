"""Common runtime behavior for Chihiros family devices."""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable, Sequence

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from ..models import DeviceKind, DeviceModel
from ..protocol.frame import create_command_encoding, encode_timestamp, next_message_id
from ..protocol.notifications import ParsedNotification
from ..registry import FALLBACK
from ..transport import BleTransport, ChihirosTransport

DEFAULT_ATTEMPTS = 3
COMMAND_NOTIFICATION_WAIT = 0.5
STATUS_NOTIFICATION_WAIT = 1.0
NotificationCallback = Callable[[ParsedNotification], None]


class BaseChihirosDevice:
    """Shared identity, transport, notification, and command runtime."""

    def __init__(
        self,
        ble_device: BLEDevice,
        model: DeviceModel = FALLBACK,
        advertisement_data: AdvertisementData | None = None,
        *,
        transport: ChihirosTransport | None = None,
    ) -> None:
        """Create a family device backed by an injected or production transport."""
        self._ble_device = ble_device
        self.model = model
        self._logger = logging.getLogger(ble_device.address.replace(":", "-"))
        self._advertisement_data = advertisement_data
        self._msg_id = next_message_id()
        self._notification_callbacks: set[NotificationCallback] = set()
        self.transport: ChihirosTransport = transport or BleTransport(
            ble_device,
            advertisement_data,
            notification_callback=self._notification_handler,
            prelude_callback=self._connection_prelude,
            logger=self._logger,
        )
        if transport is not None:
            self.transport.update_device(ble_device, advertisement_data)
            self.transport.set_callbacks(
                notification_callback=self._notification_handler,
                prelude_callback=self._connection_prelude,
            )

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
        self.transport.update_device(ble_device, advertisement_data)

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
    def device_kind(self) -> DeviceKind:
        """Return the stable profile family discriminator."""
        return self.model.device_kind

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

    def add_notification_callback(self, callback: NotificationCallback) -> Callable[[], None]:
        """Register a callback for parsed device notifications."""
        self._notification_callbacks.add(callback)

        def remove_callback() -> None:
            self._notification_callbacks.discard(callback)

        return remove_callback

    async def _send_command(
        self,
        command: list[bytes] | bytes | bytearray,
        retry: int | None = None,
        notification_wait: float | None = None,
    ) -> None:
        """Submit one complete encoded frame batch to the public transport."""
        commands_to_send = [bytes(item) for item in command] if isinstance(command, list) else [bytes(command)]
        attempts = DEFAULT_ATTEMPTS if retry is None else retry
        if attempts < 1:
            raise ValueError("retry must be at least 1")
        if notification_wait is None:
            notification_wait = COMMAND_NOTIFICATION_WAIT
        self._logger.debug("%s: Sending commands %s", self.name, [item.hex() for item in commands_to_send])
        await self.transport.send(commands_to_send, attempts=attempts, notification_wait=notification_wait)

    def _parse_notification(self, data: bytes | bytearray) -> ParsedNotification | None:
        """Parse a raw notification using the selected family codec."""
        return None

    def _notification_handler(self, _sender: object, data: bytearray) -> None:
        """Dispatch a parsed family notification to subscribers."""
        parsed = self._parse_notification(data)
        if parsed is None:
            self._logger.debug("%s: Notification received: %s", self.name, data.hex())
            return
        self._record_notification(parsed)
        self._notify_callbacks(parsed)

    async def _connection_prelude(self) -> Sequence[bytes]:
        """Build the startup batch lazily for a newly established connection."""
        prelude = [
            create_command_encoding(90, 4, self.get_next_msg_id(), [1]),
            create_command_encoding(90, 9, self.get_next_msg_id(), encode_timestamp(datetime.datetime.now())),
            create_command_encoding(90, 9, self.get_next_msg_id(), encode_timestamp(datetime.datetime.now())),
        ]
        self._logger.debug(
            "%s: Sending connection prelude %s",
            self.name,
            [command.hex() for command in prelude],
        )
        return prelude

    def _record_notification(self, parsed: ParsedNotification) -> None:
        """Log a parsed notification; family drivers own persistent state."""
        self._logger.debug("%s: %s", self.name, type(parsed).__name__)

    def _notify_callbacks(self, notification: ParsedNotification) -> None:
        """Notify subscribers about parsed device notifications."""
        for callback in tuple(self._notification_callbacks):
            try:
                callback(notification)
            except Exception:
                self._logger.exception("Notification callback failed for %s", self.name)

    async def disconnect(self) -> None:
        """Disconnect from the device through its transport."""
        await self.transport.disconnect()


__all__ = ["BaseChihirosDevice"]
