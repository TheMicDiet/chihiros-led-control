"""Scripted BLE transport for exercising the device client without hardware.

ScriptedTransport subclasses the production BleTransport and substitutes only
the BLE client. Command frames written by a family device are recorded and
matched against registered rules; matching rules deliver notification frames
through the normal notification handler, so message-id sequencing, the
connection prelude, notification parsing, and retry logic run against scripted bytes.

Example::

    import asyncio

    from chihiros_led_control.devices.led import ChihirosDevice
    from chihiros_led_control.models import WHITE_CHANNELS, DeviceModel, LedSpec
    from chihiros_led_control.testing import ScriptedBLEDevice, ScriptedTransport

    async def run() -> None:
        transport = ScriptedTransport()
        transport.expect(90, 4, [1], respond=[bytes.fromhex("5b 1b 0a 00 01 0a 01 ff")])
        device = ChihirosDevice(
            ScriptedBLEDevice(transport.name, transport.address),
            DeviceModel("Test", (), LedSpec(WHITE_CHANNELS)),
            transport=transport,
        )
        await device.query_status()
        print(device.last_runtime_notification)
        print([command.hex() for command in transport.writes])

    asyncio.run(run())
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

from bleak_retry_connector import BleakClientWithServiceCache, BleakError

from .const import (
    DFU_CHAR_UUID,
    HM10_RX_CHAR_UUID,
    UART_RX_CHAR_UUID,
    UART_TX_CHAR_UUID,
)
from .transport import BATCH_WRITE_DELAY, BleTransport, PreludeCallback, WriteBoundary

NotificationHandler = Callable[[object, bytearray], None]
DisconnectedCallback = Callable[[object], None]


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

    Exposes the Nordic UART write/notify pair, full-duplex FFE1 control, and
    the separate DFU endpoint. ``include_notify=False`` removes control
    notification support while retaining DFU to catch accidental pairing.
    """

    def __init__(self, include_notify: bool, characteristics: Mapping[str, Sequence[str]] | None) -> None:
        """Create the supplied characteristics or the default layout."""
        if characteristics is not None:
            self._characteristics = {
                uuid: _ScriptedCharacteristic(uuid, props) for uuid, props in characteristics.items()
            }
            return
        self._characteristics = {
            UART_RX_CHAR_UUID: _ScriptedCharacteristic(UART_RX_CHAR_UUID, ("write", "write-without-response")),
            HM10_RX_CHAR_UUID: _ScriptedCharacteristic(
                HM10_RX_CHAR_UUID, ("write", "write-without-response", *(("notify",) if include_notify else ()))
            ),
            DFU_CHAR_UUID: _ScriptedCharacteristic(DFU_CHAR_UUID, ("notify",)),
        }
        if include_notify:
            self._characteristics.update(
                {
                    UART_TX_CHAR_UUID: _ScriptedCharacteristic(UART_TX_CHAR_UUID, ("notify", "read")),
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

    def replies_for(self, frame: bytes) -> tuple[bytes, ...]:
        """Return reply frames or raise the matching scripted write failure."""
        for rule in self._rules:
            if not self._rule_matches(rule, frame):
                continue
            if rule.fail:
                raise BleakError("scripted write failure")
            if callable(rule.respond):
                return tuple(rule.respond(frame))
            return rule.respond
        return ()


@dataclass
class ScriptedPause:
    """A one-shot pause at the simulated GATT boundary."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def wait(self, timeout: float = 5) -> None:
        """Wait until the operation reaches the simulated peripheral."""
        await asyncio.wait_for(self.entered.wait(), timeout)


class _ScriptedBleClient:
    """In-memory GATT client that records writes and pushes scripted replies."""

    def __init__(self, transport: ScriptedTransport) -> None:
        """Initialize the scripted GATT client."""
        self._transport = transport
        self._responder = transport.responder
        self._notification_handler: NotificationHandler | None = None
        self._disconnected_callback: DisconnectedCallback | None = None
        self.is_connected = False
        self.services = _ScriptedServices(transport.notify_characteristics, transport.characteristics)

    def attach(self, disconnected_callback: DisconnectedCallback) -> None:
        """Reset state for a fresh connection and remember the disconnect callback."""
        self._notification_handler = None
        self.is_connected = True
        self._disconnected_callback = disconnected_callback

    async def start_notify(self, characteristic: _ScriptedCharacteristic, handler: NotificationHandler) -> None:
        """Register the notification handler used to deliver scripted replies."""
        self._transport.subscriptions.append(characteristic.uuid)
        await self._transport._wait_at("subscribe")
        self._notification_handler = handler

    async def stop_notify(self, characteristic: object) -> None:
        """Drop the registered notification handler."""
        del characteristic
        await self._transport._wait_at("unsubscribe")
        self._notification_handler = None

    async def write_gatt_char(
        self,
        characteristic: object,
        data: bytes | bytearray,
        response: bool = False,
    ) -> None:
        """Record a written command frame and deliver scripted replies."""
        del characteristic
        frame = bytes(data)
        self._responder.writes.append(frame)
        self._transport.write_responses.append(response)
        await self._transport._wait_at("write", frame[5] if len(frame) > 5 else None)
        for reply in self._responder.replies_for(frame):
            self.notify(reply)

    def notify(self, frame: bytes) -> None:
        """Deliver a notification to the current subscription, if any."""
        if self._notification_handler is not None:
            self._notification_handler(None, bytearray(frame))

    async def disconnect(self) -> None:
        """Disconnect and notify the device client's disconnect handler."""
        await self._transport._wait_at("disconnect")
        self.is_connected = False
        callback = self._disconnected_callback
        self._disconnected_callback = None
        if callback is not None:
            callback(self)


class ScriptedTransport(BleTransport):
    """Scripted BLE client using the production transport lifecycle."""

    def __init__(
        self,
        *,
        name: str = "DYNA2-test",
        address: str = "AA:BB:CC:DD:EE:FF",
        notify_characteristics: bool = True,
        characteristics: Mapping[str, Sequence[str]] | None = None,
        realtime: bool = True,
        notification_callback: NotificationHandler | None = None,
        prelude_callback: PreludeCallback | None = None,
    ) -> None:
        """Initialize a scripted responder and BLE session factory."""
        self.address = address
        self.notify_characteristics = notify_characteristics
        self.characteristics = characteristics
        self.realtime = realtime
        self.responder = ScriptedResponder()
        self.connections = 0
        self.clients: list[_ScriptedBleClient] = []
        self.subscriptions: list[str] = []
        self.write_responses: list[bool] = []
        self._pauses: dict[tuple[str, int | None], ScriptedPause] = {}
        super().__init__(
            ScriptedBLEDevice(name, address),  # type: ignore[arg-type]
            notification_callback=notification_callback,
            prelude_callback=prelude_callback,
        )

    @property
    def writes(self) -> list[bytes]:
        """Return every command frame written by this transport."""
        return self.responder.writes

    @property
    def batch_write_delay(self) -> float:
        """Return the scriptable production pacing interval."""
        return BATCH_WRITE_DELAY if self.realtime else 0

    async def send(
        self, frames: Sequence[bytes], *, attempts: int, notification_wait: float, boundary: WriteBoundary | None = None
    ) -> None:
        """Send through the real transport, optionally skipping hardware-only delays."""
        await super().send(
            frames, attempts=attempts, notification_wait=notification_wait if self.realtime else 0, boundary=boundary
        )

    def notify(self, frame: bytes) -> None:
        """Send an unsolicited notification from the connected peripheral."""
        if self.clients and self.clients[-1].is_connected:
            self.clients[-1].notify(frame)

    def pause(self, operation: str, *, mode: int | None = None) -> ScriptedPause:
        """Pause the next matching connect, subscribe, write, unsubscribe or disconnect."""
        pause = ScriptedPause()
        self._pauses[operation, mode] = pause
        return pause

    async def _wait_at(self, operation: str, mode: int | None = None) -> None:
        pause = self._pauses.pop((operation, mode), None) or self._pauses.pop((operation, None), None)
        if pause is not None:
            pause.entered.set()
            await pause.release.wait()

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

    async def _establish_ble_client(self) -> BleakClientWithServiceCache:
        """Create the next scripted BLE session for production setup to configure."""
        await self._wait_at("connect")
        client = _ScriptedBleClient(self)
        client.attach(self._disconnected)
        self.clients.append(client)
        self.connections += 1
        return cast(BleakClientWithServiceCache, client)
