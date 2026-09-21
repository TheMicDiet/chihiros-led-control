"""Scripted BLE transport for exercising the device client without hardware.

The scripted transport implements ``ChihirosTransport`` directly. Command
frames written by a family device are recorded and matched against registered
rules; matching rules deliver notification frames through the normal
notification handler, so message-id sequencing, the connection prelude,
notification parsing, and retry logic run against scripted bytes.

Example::

    import asyncio

    from chihiros_led_control.devices.led import ChihirosDevice
    from chihiros_led_control.models import WHITE_CHANNELS, DeviceModel

    async def run() -> None:
        transport = ScriptedTransport()
        transport.expect(90, 4, [1], respond=[bytes.fromhex("5b 1b 0a 00 01 0a 01 ff")])
        device = transport.make_device(DeviceModel("Test", (), WHITE_CHANNELS))
        await device.query_status()
        print(device.last_runtime_notification)
        print([command.hex() for command in transport.writes])

    asyncio.run(run())
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from inspect import isawaitable

from bleak_retry_connector import BLEAK_RETRY_EXCEPTIONS as BLEAK_EXCEPTIONS
from bleak_retry_connector import BleakError

from .const import (
    CUSTOM_NOTIFY_CHAR_UUID,
    HM10_RX_CHAR_UUID,
    UART_RX_CHAR_UUID,
    UART_TX_CHAR_UUID,
)
from .exceptions import CharacteristicMissingError
from .models import DeviceModel
from .registry import DOSING_PUMP, FALLBACK, HEATER, MAG_STIRRER
from .transport import BATCH_WRITE_DELAY, PreludeCallback, pair_notify_characteristic

NotificationHandler = Callable[[object, bytearray], None]
DisconnectedCallback = Callable[[object], None]

# Sentinel returned by the responder for rules marked ``fail``.
_FAIL = object()


class ScriptedBLEDevice:
    """Minimal ``BLEDevice`` stand-in with a name and address."""

    def __init__(self, name: str, address: str) -> None:
        """Initialize the scripted BLE device metadata."""
        self.name = name
        self.address = address


class _ScriptedCharacteristic:
    """GATT characteristic stand-in exposing a UUID and its properties."""

    def __init__(self, uuid: str, properties: Sequence[str] = ()) -> None:
        """Initialize the characteristic with its UUID and property set."""
        self.uuid = uuid
        self.properties = frozenset(properties)


class _ScriptedServices:
    """GATT service collection answering for Chihiros UART UUIDs.

    Exposes the Nordic UART pair (NUS) plus the classic BleLed layouts: FFE1
    write + 8ec90003 notify and a full-duplex FFE1 (notify + write +
    write-without-response) used by devices with no separate notify endpoint.
    With ``include_notify=False`` the notify-only characteristics are omitted,
    emulating devices that force the client into fire-and-forget mode.
    """

    def __init__(self, *, include_notify: bool = True) -> None:
        """Create the UART TX/RX characteristics."""
        self._characteristics = {
            UART_RX_CHAR_UUID: _ScriptedCharacteristic(UART_RX_CHAR_UUID, ("write", "write-without-response")),
            HM10_RX_CHAR_UUID: _ScriptedCharacteristic(
                HM10_RX_CHAR_UUID, ("notify", "write", "write-without-response")
            ),
        }
        if include_notify:
            self._characteristics.update(
                {
                    UART_TX_CHAR_UUID: _ScriptedCharacteristic(UART_TX_CHAR_UUID, ("notify", "read")),
                    CUSTOM_NOTIFY_CHAR_UUID: _ScriptedCharacteristic(CUSTOM_NOTIFY_CHAR_UUID, ("notify",)),
                }
            )

    def get_characteristic(self, uuid: str) -> _ScriptedCharacteristic | None:
        """Return the characteristic with ``uuid``, if present."""
        return self._characteristics.get(uuid)


@dataclass(frozen=True)
class _ScriptRule:
    """One command-matching rule with its reply frames."""

    cmd_id: int
    cmd_mode: int
    params: tuple[int, ...] | None
    respond: tuple[bytes, ...] | Callable[[bytes], Sequence[bytes]]
    fail: bool


class ScriptedResponder:
    """Record written command frames and answer them from registered rules.

    Frames are matched on the command id (``frame[0]``), the mode byte
    (``frame[5]``), and optionally a parameter prefix (``frame[6:]``). Message
    id bytes and the trailing checksum are ignored so rules stay stable across
    a session.
    """

    def __init__(self) -> None:
        """Initialize the responder with no rules and no writes."""
        self.writes: list[bytes] = []
        self._rules: list[_ScriptRule] = []

    def expect(
        self,
        cmd_id: int,
        cmd_mode: int,
        params: Sequence[int] | None = None,
        *,
        respond: Sequence[bytes] | Callable[[bytes], Sequence[bytes]] = (),
        fail: bool = False,
    ) -> None:
        """Register a reply for command frames with ``cmd_id`` and ``cmd_mode``.

        ``params`` restricts matching to frames whose parameter bytes start
        with the given sequence (``None`` matches any parameters). ``respond``
        frames are delivered to the client as notifications after the write; a
        callable receives the written frame and returns the reply frames (and
        may raise ``BleakError`` to fail the write). ``fail`` makes every
        matching write raise ``BleakError`` to simulate BLE failures.
        """
        self._rules.append(
            _ScriptRule(
                cmd_id=cmd_id,
                cmd_mode=cmd_mode,
                params=tuple(params) if params is not None else None,
                respond=respond,
                fail=fail,
            )
        )

    @staticmethod
    def _params_match(rule: _ScriptRule, frame: bytes) -> bool:
        """Return whether the frame's parameter bytes start with the rule's."""
        if rule.params is None:
            return True
        return frame[6 : 6 + len(rule.params)] == bytes(rule.params)

    def _rule_matches(self, rule: _ScriptRule, frame: bytes) -> bool:
        """Return whether a written frame matches a registered rule."""
        return frame[0] == rule.cmd_id and frame[5] == rule.cmd_mode and self._params_match(rule, frame)

    def replies_for(self, frame: bytes) -> tuple[bytes, ...] | object | None:
        """Return reply frames for a written command.

        Returns ``_FAIL`` when the matching rule simulates a write failure,
        ``None`` when no rule matches, and a tuple of reply frames otherwise.
        """
        for rule in self._rules:
            if not self._rule_matches(rule, frame):
                continue
            if rule.fail:
                return _FAIL
            if callable(rule.respond):
                return tuple(rule.respond(frame))
            return rule.respond
        return None


class _ScriptedBleClient:
    """In-memory GATT client that records writes and pushes scripted replies."""

    def __init__(self, responder: ScriptedResponder, *, include_notify: bool = True) -> None:
        """Initialize the scripted GATT client."""
        self._responder = responder
        self._notification_handler: NotificationHandler | None = None
        self._disconnected_callback: DisconnectedCallback | None = None
        self.is_connected = False
        self.services = _ScriptedServices(include_notify=include_notify)

    def attach(self, disconnected_callback: DisconnectedCallback) -> None:
        """Reset state for a fresh connection and remember the disconnect callback."""
        self._notification_handler = None
        self.is_connected = True
        self._disconnected_callback = disconnected_callback

    async def get_services(self) -> _ScriptedServices:
        """Return the scripted service collection."""
        return self.services

    async def start_notify(self, characteristic: object, handler: NotificationHandler) -> None:
        """Register the notification handler used to deliver scripted replies."""
        del characteristic
        self._notification_handler = handler

    async def stop_notify(self, characteristic: object) -> None:
        """Drop the registered notification handler."""
        del characteristic
        self._notification_handler = None

    async def write_gatt_char(
        self,
        characteristic: object,
        data: bytes | bytearray,
        response: bool = False,
    ) -> None:
        """Record a written command frame and deliver scripted replies."""
        del characteristic, response
        frame = bytes(data)
        self._responder.writes.append(frame)
        replies = self._responder.replies_for(frame)
        if replies is _FAIL:
            raise BleakError("scripted write failure")
        handler = self._notification_handler
        if handler is not None and replies:
            for reply in replies:
                # The client's notification handler ignores the sender.
                handler(None, bytearray(reply))

    async def disconnect(self) -> None:
        """Disconnect and notify the device client's disconnect handler."""
        self.is_connected = False
        callback = self._disconnected_callback
        self._disconnected_callback = None
        if callback is not None:
            callback(self)


class ScriptedTransport:
    """In-memory transport implementing the production transport contract."""

    def __init__(
        self,
        *,
        name: str = "DYNA2-test",
        address: str = "AA:BB:CC:DD:EE:FF",
        notify_characteristics: bool = True,
        notification_callback: NotificationHandler | None = None,
        prelude_callback: PreludeCallback | None = None,
    ) -> None:
        """Initialize a scripted responder and a fresh GATT session."""
        self.name = name
        self.address = address
        self.ble_device = ScriptedBLEDevice(name, address)
        self.advertisement_data: object | None = None
        self.responder = ScriptedResponder()
        self.connections = 0
        self._client = _ScriptedBleClient(self.responder, include_notify=notify_characteristics)
        self._logger = logging.getLogger(self.address.replace(":", "-"))
        self._notification_callback = notification_callback
        self._prelude_callback = prelude_callback
        self._read_char: _ScriptedCharacteristic | None = None
        self._write_char: _ScriptedCharacteristic | None = None
        self._connect_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._unexpected_disconnect = asyncio.Event()
        self._expected_disconnect = False
        self._disconnect_timer: asyncio.TimerHandle | None = None
        self._disconnect_timer_generation = 0
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def writes(self) -> list[bytes]:
        """Return every command frame written by this transport."""
        return self.responder.writes

    @property
    def is_connected(self) -> bool:
        """Return whether the scripted GATT client is connected."""
        return self._client.is_connected

    def set_callbacks(
        self,
        *,
        notification_callback: NotificationHandler,
        prelude_callback: PreludeCallback,
    ) -> None:
        """Bind raw notification and lazy connection-prelude callbacks."""
        self._notification_callback = notification_callback
        self._prelude_callback = prelude_callback

    def expect(
        self,
        cmd_id: int,
        cmd_mode: int,
        params: Sequence[int] | None = None,
        *,
        respond: Sequence[bytes] | Callable[[bytes], Sequence[bytes]] = (),
        fail: bool = False,
    ) -> None:
        """Register a scripted reply for matching command frames."""
        self.responder.expect(cmd_id, cmd_mode, params, respond=respond, fail=fail)

    def _scripted_device(self) -> ScriptedBLEDevice:
        """Return a BLE identity suitable for a package family constructor."""
        return ScriptedBLEDevice(self.name, self.address)

    def make_device(self, model: DeviceModel = FALLBACK):
        """Create an LED family client bound directly to this transport."""
        from .devices.led import ChihirosDevice

        return ChihirosDevice(self._scripted_device(), model, transport=self)

    def make_pump(self, model: DeviceModel = DOSING_PUMP):
        """Create a dosing-pump family client bound directly to this transport."""
        from .devices.dosing import ChihirosDosingPump

        return ChihirosDosingPump(self._scripted_device(), model, transport=self)

    def make_stirrer(self, model: DeviceModel = MAG_STIRRER):
        """Create a magnetic-stirrer family client bound directly to this transport."""
        from .devices.stirrer import ChihirosMagStirrer

        return ChihirosMagStirrer(self._scripted_device(), model, transport=self)

    def make_heater(self, model: DeviceModel = HEATER):
        """Create a heater family client bound directly to this transport."""
        from .devices.heater import ChihirosHeater

        return ChihirosHeater(self._scripted_device(), model, transport=self)

    async def _ensure_connected(self) -> None:
        """Connect and configure the scripted GATT client when idle."""
        if self._client.is_connected:
            return
        async with self._connect_lock:
            if self._client.is_connected:
                return
            await self._connect_client()

    async def _connect_client(self) -> None:
        """Create and configure one scripted GATT session."""
        self._expected_disconnect = False
        self._unexpected_disconnect.clear()
        self._client.attach(self._disconnected)
        self.connections += 1
        try:
            await self._configure_connected_client()
        except Exception:
            await self._abort_connection()
            raise

    async def _configure_connected_client(self) -> None:
        """Resolve scripted endpoints, notifications, and the connection prelude."""
        services = await self._client.get_services()
        self._write_char = services.get_characteristic(HM10_RX_CHAR_UUID) or services.get_characteristic(
            UART_RX_CHAR_UUID
        )
        self._read_char = pair_notify_characteristic(services, self._write_char)
        if self._write_char is None:
            raise CharacteristicMissingError("Write characteristic missing")
        if self._read_char is not None:
            await self._client.start_notify(self._read_char, self._notification_handler)
        else:
            self._logger.warning(
                "%s: No notify characteristic (8ec90003/6e400003) found; "
                "running fire-and-forget without state notifications",
                self.name,
            )
        await self._run_prelude()

    async def _run_prelude(self) -> None:
        """Run the configured scripted connection prelude."""
        if self._prelude_callback is None:
            return
        prelude = self._prelude_callback()
        if isawaitable(prelude):
            prelude = await prelude
        await self._write_frames(prelude)

    async def _send_once(self, frames: Sequence[bytes], notification_wait: float) -> None:
        """Write one complete scripted transaction."""
        await self._ensure_connected()
        await self._write_frames(frames)
        if notification_wait:
            await asyncio.sleep(notification_wait)

    async def send(self, frames: Sequence[bytes], *, attempts: int, notification_wait: float) -> None:
        """Connect lazily, run the prelude once, and write a frame batch."""
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        commands_to_send = [bytes(frame) for frame in frames]
        if not commands_to_send:
            return
        async with self._operation_lock:
            await self._send_attempts(commands_to_send, attempts, notification_wait)

    async def _send_attempts(self, frames: Sequence[bytes], attempts: int, notification_wait: float) -> None:
        """Run scripted transaction attempts while holding the operation lock."""
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
        """Run one scripted transaction attempt and report whether it succeeded."""
        try:
            await self._send_once(frames, notification_wait)
        except (CharacteristicMissingError, asyncio.CancelledError):
            await self._execute_disconnect()
            raise
        except BLEAK_EXCEPTIONS:
            await self._execute_disconnect()
            if attempt == attempts:
                raise
            return False
        self._schedule_disconnect_timer()
        return True

    def _notification_handler(self, sender: object, data: bytearray) -> None:
        """Forward raw scripted notifications to the family driver."""
        if self._notification_callback is not None:
            self._notification_callback(sender, data)

    def _disconnected(self, client: object) -> None:
        """Handle expected and unexpected scripted disconnects."""
        if client is not self._client:
            return
        self._cancel_disconnect_timer()
        self._read_char = None
        self._write_char = None
        if self._expected_disconnect:
            return
        self._unexpected_disconnect.set()

    async def _write_frames(self, frames: Sequence[bytes]) -> None:
        """Write scripted frame batches with production pacing."""
        if self._write_char is None:
            raise CharacteristicMissingError("Write characteristic missing")
        for index, frame in enumerate(frames):
            await self._write_frame(frame)
            if index < len(frames) - 1:
                await asyncio.sleep(BATCH_WRITE_DELAY)

    async def _write_frame(self, frame: bytes) -> None:
        """Write one scripted frame while checking for a disconnect."""
        if self._unexpected_disconnect.is_set():
            raise BleakError("Device unexpectedly disconnected during command batch")
        await self._client.write_gatt_char(self._write_char, frame, False)
        if self._unexpected_disconnect.is_set():
            raise BleakError("Device unexpectedly disconnected during command batch")

    async def _abort_connection(self) -> None:
        """Tear down a partially configured scripted connection."""
        self._expected_disconnect = True
        read_char = self._read_char
        self._read_char = None
        self._write_char = None
        self._cancel_disconnect_timer()
        if read_char is not None:
            await self._client.stop_notify(read_char)
        if self._client.is_connected:
            await self._client.disconnect()

    async def disconnect(self) -> None:
        """Disconnect the active scripted GATT session."""
        async with self._operation_lock:
            await self._execute_disconnect()

    async def _execute_disconnect(self) -> None:
        """Disconnect while holding the connection lock."""
        async with self._connect_lock:
            self._expected_disconnect = True
            read_char = self._read_char
            self._read_char = None
            self._write_char = None
            self._cancel_disconnect_timer()
            if read_char is not None:
                await self._client.stop_notify(read_char)
            if self._client.is_connected:
                await self._client.disconnect()

    def _cancel_disconnect_timer(self) -> None:
        """Cancel a pending idle disconnect."""
        self._disconnect_timer_generation += 1
        if self._disconnect_timer is not None:
            self._disconnect_timer.cancel()
            self._disconnect_timer = None

    def _schedule_disconnect_timer(self) -> None:
        """Schedule disconnect after the production idle timeout."""
        self._cancel_disconnect_timer()
        if not self._client.is_connected:
            return
        generation = self._disconnect_timer_generation
        loop = self._loop or asyncio.get_running_loop()
        self._loop = loop
        self._disconnect_timer = loop.call_later(
            120,
            self._disconnect_after_timeout,
            generation,
        )

    def _disconnect_after_timeout(self, generation: int) -> None:
        """Start idle disconnect work from the event-loop timer."""
        if generation != self._disconnect_timer_generation:
            return
        self._disconnect_timer = None
        self._loop.create_task(self._execute_timed_disconnect(generation))

    async def _execute_timed_disconnect(self, generation: int) -> None:
        """Disconnect only if no newer operation refreshed the deadline."""
        async with self._operation_lock:
            if generation != self._disconnect_timer_generation or not self._client.is_connected:
                return
            await self._execute_disconnect()

    def update_device(self, ble_device: object, advertisement_data: object) -> None:
        """Record the latest device identity for protocol conformance."""
        self.ble_device = ble_device
        self.advertisement_data = advertisement_data
        self.name = getattr(ble_device, "name", self.name)
        self.address = getattr(ble_device, "address", self.address)
