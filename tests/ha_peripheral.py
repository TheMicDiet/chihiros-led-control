"""In-memory GATT peripherals registered as a Home Assistant Bluetooth proxy."""

from __future__ import annotations

from functools import partial
from time import monotonic

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.client import BaseBleakClient, NotifyCallback
from bleak.backends.device import BLEDevice
from bleak.backends.service import BleakGATTService, BleakGATTServiceCollection
from bleak.exc import BleakError
from habluetooth import BaseHaRemoteScanner, HaBluetoothConnector

from chihiros_led_control.const import HM10_RX_CHAR_UUID
from chihiros_led_control.testing import ScriptedResponder


class PeripheralClient(BaseBleakClient):
    """Bleak backend for the in-memory GATT server advertised by the proxy."""

    def __init__(self, peripherals: dict[str, ScriptedResponder], device: BLEDevice, **kwargs) -> None:
        """Use the peripheral associated with this advertisement."""
        super().__init__(device, **kwargs)
        self.peripheral = peripherals[device.address]
        self.device_name = device.name
        self.connected = False
        self.notification: NotifyCallback | None = None

    @property
    def name(self) -> str:
        """Return the advertised name."""
        return self.device_name or self.address

    @property
    def mtu_size(self) -> int:
        """Return the negotiated MTU."""
        return 185

    @property
    def is_connected(self) -> bool:
        """Report whether the peripheral has an active connection."""
        return self.connected

    async def connect(self, pair: bool, **kwargs) -> None:
        """Open the session with the full-duplex FFE1 endpoint."""
        self.connected = True
        self.services = BleakGATTServiceCollection()
        service = BleakGATTService(None, 1, "0000ffe0-0000-1000-8000-00805f9b34fb")
        self.services.add_service(service)
        characteristic = BleakGATTCharacteristic(
            None, 2, HM10_RX_CHAR_UUID.lower(), ["write", "notify"], lambda: 182, service
        )
        self.services.add_characteristic(characteristic)

    async def disconnect(self) -> None:
        """Close the session and drop its subscription."""
        self.connected = False
        self.notification = None

    async def start_notify(self, characteristic, callback, **kwargs) -> None:
        """Subscribe to the peripheral's replies."""
        self.notification = callback

    async def stop_notify(self, characteristic) -> None:
        """Unsubscribe from replies."""
        self.notification = None

    async def write_gatt_char(self, characteristic, data, response: bool) -> None:
        """Record accepted writes and return configured replies over GATT."""
        frame = bytes(data)
        self.peripheral.writes.append(frame)
        for reply in self.peripheral.replies_for(frame):
            if self.notification is not None:
                self.notification(bytearray(reply))

    async def read_gatt_char(self, characteristic, **kwargs) -> bytearray:
        """Reject reads from the write/notify endpoint."""
        raise BleakError("Characteristic is not readable")

    async def read_gatt_descriptor(self, descriptor, **kwargs) -> bytearray:
        """Reject descriptor reads on this peripheral."""
        raise BleakError("No readable descriptor")

    async def write_gatt_descriptor(self, descriptor, data) -> None:
        """Reject descriptor writes on this peripheral."""
        raise BleakError("No writable descriptor")

    async def pair(self, *args, **kwargs) -> None:
        """Pairing is not available on the simulated peripheral."""
        raise BleakError("Pairing is not supported")

    async def unpair(self) -> None:
        """Pairing is not available on the simulated peripheral."""
        raise BleakError("Pairing is not supported")


class PeripheralProxy(BaseHaRemoteScanner):
    """A Bluetooth proxy that advertises and connects to scripted peripherals."""

    def __init__(self) -> None:
        """Register a connectable source like a real Bluetooth proxy."""
        source = "scripted-proxy"
        self.peripherals: dict[str, ScriptedResponder] = {}
        connector = HaBluetoothConnector(partial(PeripheralClient, self.peripherals), source, lambda: True)
        super().__init__(source, source, connector, connectable=True)

    def advertise(self, address: str, name: str, peripheral: ScriptedResponder) -> None:
        """Send a peripheral advertisement to Home Assistant's normal scanner callback."""
        self.peripherals[address] = peripheral
        self._async_on_advertisement(address, -50, name, [], {}, {}, None, {}, monotonic())
