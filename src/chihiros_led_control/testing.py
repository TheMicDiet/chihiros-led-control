"""Scripted BLE transport for exercising the device client without hardware.

The scripted transport replaces ``chihiros_led_control.client.establish_connection``
with an in-memory GATT connection. Command frames written by the real
``ChihirosDevice`` are recorded and matched against registered rules; matching
rules deliver notification frames through the normal notification handler, so
message-id sequencing, the connection prelude, notification parsing, and retry
logic all run against scripted bytes.

Example::

    import asyncio

    from chihiros_led_control.client import ChihirosDevice
    from chihiros_led_control.models import WHITE_CHANNELS, DeviceModel
    from chihiros_led_control.testing import ScriptedTransport

    async def run() -> None:
        transport = ScriptedTransport()
        transport.expect(90, 4, [1], respond=[bytes.fromhex("5b 1b 0a 00 01 0a 01 ff")])
        device = transport.make_device(DeviceModel("Test", (), WHITE_CHANNELS))
        with transport.patch_establish_connection():
            await device.query_status()
        print(device.last_runtime_notification)
        print([command.hex() for command in transport.writes])

    asyncio.run(run())
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from bleak_retry_connector import BleakClientWithServiceCache, BleakError

from . import client as _client_module
from .const import (
    HM10_RX_CHAR_UUID,
    HM10_TX_CHAR_UUID,
    UART_RX_CHAR_UUID,
    UART_TX_CHAR_UUID,
)
from .models import DOSING_PUMP, FALLBACK, DeviceModel

NotificationHandler = Callable[[object, bytearray], None]
DisconnectedCallback = Callable[[BleakClientWithServiceCache], None]

# Sentinel returned by the responder for rules marked ``fail``.
_FAIL = object()


class ScriptedBLEDevice:
    """Minimal ``BLEDevice`` stand-in with a name and address."""

    def __init__(self, name: str, address: str) -> None:
        """Initialize the scripted BLE device metadata."""
        self.name = name
        self.address = address


class _ScriptedCharacteristic:
    """GATT characteristic stand-in exposing a UUID."""

    def __init__(self, uuid: str) -> None:
        """Initialize the characteristic with its UUID."""
        self.uuid = uuid


class _ScriptedServices:
    """GATT service collection answering for Chihiros UART UUIDs (Nordic + HM-10)."""

    def __init__(self) -> None:
        """Create the UART TX/RX characteristics."""
        self._characteristics = {
            UART_TX_CHAR_UUID: _ScriptedCharacteristic(UART_TX_CHAR_UUID),
            UART_RX_CHAR_UUID: _ScriptedCharacteristic(UART_RX_CHAR_UUID),
            HM10_TX_CHAR_UUID: _ScriptedCharacteristic(HM10_TX_CHAR_UUID),
            HM10_RX_CHAR_UUID: _ScriptedCharacteristic(HM10_RX_CHAR_UUID),
        }

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

    def __init__(self, responder: ScriptedResponder) -> None:
        """Initialize the scripted GATT client."""
        self._responder = responder
        self._notification_handler: NotificationHandler | None = None
        self._disconnected_callback: DisconnectedCallback | None = None
        self.is_connected = False
        self.services = _ScriptedServices()

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
    """Bundle a scripted responder, GATT client, and ``establish_connection`` stand-in."""

    def __init__(self, *, name: str = "DYNA2-test", address: str = "AA:BB:CC:DD:EE:FF") -> None:
        """Initialize a transport with a fresh responder and client."""
        self.name = name
        self.address = address
        self.responder = ScriptedResponder()
        self.connections = 0
        self._client = _ScriptedBleClient(self.responder)

    @property
    def writes(self) -> list[bytes]:
        """Return every command frame the device client has written."""
        return self.responder.writes

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

    def make_device(self, model: DeviceModel = FALLBACK) -> _client_module.ChihirosDevice:
        """Create a device client bound to this scripted transport.

        Must be called inside a running asyncio loop (like the real client).
        """
        return _client_module.ChihirosDevice(ScriptedBLEDevice(self.name, self.address), model)

    def make_pump(self, model: DeviceModel = DOSING_PUMP) -> _client_module.ChihirosDosingPump:
        """Create a dosing pump client bound to this scripted transport."""
        return _client_module.ChihirosDosingPump(ScriptedBLEDevice(self.name, self.address), model)

    async def connect(
        self,
        _client_class: type[Any],
        _ble_device: object,
        _name: str,
        disconnected_callback: DisconnectedCallback,
        **_kwargs: object,
    ) -> _ScriptedBleClient:
        """Act as ``establish_connection`` for the device client."""
        self.connections += 1
        self._client.attach(disconnected_callback)
        return self._client

    @contextmanager
    def patch_establish_connection(self) -> "ScriptedTransport":
        """Context manager routing client connections through this transport."""
        original = _client_module.establish_connection
        _client_module.establish_connection = self.connect  # type: ignore[method-assign]
        try:
            yield self
        finally:
            _client_module.establish_connection = original
