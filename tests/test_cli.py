"""Command-line arguments and output through real device drivers."""

from __future__ import annotations

import pytest
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakError
from click import unstyle
from typer.testing import CliRunner

from chihiros_led_control.cli import BluetoothAccess, CliDevice, app
from chihiros_led_control.devices import ChihirosDevice, ChihirosDosingPump, ChihirosHeater, ChihirosMagStirrer
from chihiros_led_control.exceptions import DeviceNotFound
from chihiros_led_control.models import DeviceKind
from chihiros_led_control.registry import detect_model
from chihiros_led_control.testing import ScriptedTransport

pytestmark = pytest.mark.component
RUNNER = CliRunner()
ADDRESS = "AA:BB:CC:DD:EE:FF"


class BluetoothPeripheral:
    """A discoverable fixture peripheral with the real driver and GATT transport."""

    def __init__(self, name: str) -> None:
        """Advertise one Chihiros device at the test Bluetooth address."""
        self.device = BLEDevice(ADDRESS, name, None)
        self.ble = ScriptedTransport(name=name, realtime=False)
        self.access = BluetoothAccess(connect=self.connect, discover=self.discover)

    async def connect(self, address: str) -> CliDevice:
        """Return the appropriate real driver for this peripheral."""
        if address != self.device.address:
            raise DeviceNotFound(address)
        model = detect_model(self.device.name)
        driver = {
            DeviceKind.LED: ChihirosDevice,
            DeviceKind.DOSING_PUMP: ChihirosDosingPump,
            DeviceKind.MAG_STIRRER: ChihirosMagStirrer,
            DeviceKind.HEATER: ChihirosHeater,
        }[model.device_kind]
        return driver(self.device, model, transport=self.ble)

    async def discover(self, _timeout: int) -> list[BLEDevice]:
        """Return the peripheral currently advertising."""
        return [self.device]


def test_list_devices_prints_model_and_address() -> None:
    """Discovery presents the advertised name, address and detected model."""
    peripheral = BluetoothPeripheral("DYMIXR-test")
    result = RUNNER.invoke(app, ["list-devices"], obj=peripheral.access)
    assert result.exit_code == 0, result.output
    assert all(text in result.output for text in ("DYMIXR-test", ADDRESS, "Mag Stirrer"))


def test_program_translates_one_based_channel_and_weekdays() -> None:
    """CLI input becomes an ordered pump program on a single session."""
    peripheral = BluetoothPeripheral("DYDOSE-test")
    result = RUNNER.invoke(
        app,
        ["dosing", "program", ADDRESS, "2", "08:30:2.5", "--daily-ml", "10", "--weekdays", "monday"],
        obj=peripheral.access,
    )
    assert result.exit_code == 0, result.output or result.exception
    assert [frame[5:-1].hex(" ") for frame in peripheral.ble.writes if frame[0] == 165] == [
        "20 01 00 01",
        "1b 01 20 01 00 00 64",
        "15 01 03 08 1e 00 19",
    ]
    assert peripheral.ble.connections == 1
    assert not peripheral.ble.is_connected


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["dosing", "program", ADDRESS, "1", "--weekdays", "monday"], "requires --daily-ml"),
        (["dosing", "schedule", ADDRESS, "1", "25:00:1"], "expected HH:MM:ML"),
        (["stirrer", "schedule", ADDRESS, "1", "08:00:0"], "between 1 and 999 minutes"),
        (["heater", "manual-set", ADDRESS, "27", "201"], "divisible by 10 watts"),
    ],
)
def test_cli_rejects_invalid_input_before_using_bluetooth(args: list[str], message: str) -> None:
    """These local errors do not require a discovered device or any BLE I/O."""
    result = RUNNER.invoke(app, args)
    assert result.exit_code == 2
    assert message in unstyle(result.output)


def test_cli_rejects_wrong_family() -> None:
    """A dosing command cannot talk to a stirrer, despite their shared framing."""
    peripheral = BluetoothPeripheral("DYMIXR-test")
    result = RUNNER.invoke(app, ["dosing", "dose", ADDRESS, "1", "2.5"], obj=peripheral.access)
    assert result.exit_code == 2
    assert "not a dosing pump" in unstyle(result.output)
    assert peripheral.ble.writes == []


def test_cli_surfaces_failed_dose_without_repeating_it() -> None:
    """A command-level error leaves the ambiguous physical dose unrepeated."""
    peripheral = BluetoothPeripheral("DYDOSE-test")
    peripheral.ble.expect(165, 27, [0], fail=True)
    result = RUNNER.invoke(app, ["dosing", "dose", ADDRESS, "1", "2.5"], obj=peripheral.access)
    assert result.exit_code == 1
    assert isinstance(result.exception, BleakError)
    assert [frame[6:-1].hex(" ") for frame in peripheral.ble.writes if frame[5] == 27] == ["00 00 02 00 19"]
    assert peripheral.ble.connections == 1
    assert not peripheral.ble.is_connected
