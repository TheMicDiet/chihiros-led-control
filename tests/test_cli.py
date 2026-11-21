"""Tests for the Chihiros CLI."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from click import unstyle
from typer.testing import CliRunner

from chihiros_led_control import cli
from chihiros_led_control.devices import ChihirosDevice, ChihirosDosingPump
from chihiros_led_control.models import WHITE_CHANNELS, DeviceKind, DeviceModel, LedSpec
from chihiros_led_control.registry import DOSING_PUMP


class FakeBLEDevice:
    """Small BLEDevice stand-in for CLI tests."""

    def __init__(self, name: str = "DYDOSE-test", address: str = "AA:BB:CC:DD:EE:FF") -> None:
        """Initialize fake BLE metadata."""
        self.name = name
        self.address = address


class TrackingCliDevice:
    """Small async device stand-in for CLI command tests."""

    name = "Test Light"
    device_kind = DeviceKind.LED

    def __init__(self) -> None:
        """Initialize recorded calls."""
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.disconnects = 0

    async def turn_on(self) -> None:
        """Record turn-on calls."""
        self.calls.append(("turn_on", (), {}))

    async def turn_off(self) -> None:
        """Record turn-off calls."""
        self.calls.append(("turn_off", (), {}))

    async def set_brightness(self, brightness: list[int]) -> None:
        """Record brightness calls."""
        self.calls.append(("set_brightness", (brightness,), {}))

    async def add_setting(self, **kwargs: Any) -> None:
        """Record add-setting calls."""
        self.calls.append(("add_setting", (), kwargs))

    async def remove_setting(self, **kwargs: Any) -> None:
        """Record remove-setting calls."""
        self.calls.append(("remove_setting", (), kwargs))

    async def reset_settings(self) -> None:
        """Record reset-setting calls."""
        self.calls.append(("reset_settings", (), {}))

    async def enable_auto_mode(self) -> None:
        """Record auto-mode calls."""
        self.calls.append(("enable_auto_mode", (), {}))

    async def disconnect(self) -> None:
        """Record explicit CLI cleanup."""
        self.disconnects += 1


RUNNER = CliRunner()
TEST_ADDRESS = "AA:BB:CC:DD:EE:FF"


def _patch_device(monkeypatch: pytest.MonkeyPatch) -> TrackingCliDevice:
    """Patch CLI device resolution and return the tracking device."""
    device = TrackingCliDevice()

    async def get_device_from_address(address: str) -> TrackingCliDevice:
        assert address == TEST_ADDRESS
        return device

    monkeypatch.setattr(cli, "get_device_from_address", get_device_from_address)
    return device


@pytest.mark.component
def test_list_devices_cli_prints_detected_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """The list-devices command prints discovered devices and detected model names."""

    async def discover(timeout: int) -> list[FakeBLEDevice]:
        assert timeout == 7
        return [FakeBLEDevice("DYNWRGB-test", TEST_ADDRESS), FakeBLEDevice("UNKNOWN", "11:22:33:44:55:66")]

    monkeypatch.setattr(cli.BleakScanner, "discover", discover)

    result = RUNNER.invoke(cli.app, ["list-devices", "--timeout", "7"])

    assert result.exit_code == 0
    assert "DYNWRGB-test" in result.output
    assert "WRGB II" in result.output
    assert "UNKNOWN" in result.output
    assert "???" in result.output


@pytest.mark.component
def test_turn_on_cli_drives_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """The turn-on command calls the device turn_on API."""
    device = _patch_device(monkeypatch)

    result = RUNNER.invoke(cli.app, ["turn-on", TEST_ADDRESS])
    assert result.exit_code == 0

    assert device.calls == [("turn_on", (), {})]
    assert device.disconnects == 1


@pytest.mark.unit
def test_cli_disconnects_device_when_command_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI cleanup runs when a device command raises."""
    device = _patch_device(monkeypatch)

    async def failing_command(_device: ChihirosDevice) -> None:
        raise RuntimeError("command failed")

    with pytest.raises(RuntimeError, match="command failed"):
        cli._run_device_func(TEST_ADDRESS, failing_command)

    assert device.disconnects == 1


@pytest.mark.component
def test_light_cli_rejects_non_led_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """Top-level light commands fail clearly for non-LED devices."""
    device = ChihirosDosingPump(FakeBLEDevice(address=TEST_ADDRESS), DOSING_PUMP)  # type: ignore[arg-type]

    async def get_device_from_address(address: str) -> ChihirosDosingPump:
        assert address == TEST_ADDRESS
        return device

    monkeypatch.setattr(cli, "get_device_from_address", get_device_from_address)

    result = RUNNER.invoke(cli.app, ["turn-on", TEST_ADDRESS])

    assert result.exit_code != 0
    assert "not a light" in result.output


@pytest.mark.component
def test_turn_off_cli_drives_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """The turn-off command calls the device turn_off API."""
    device = _patch_device(monkeypatch)

    result = RUNNER.invoke(cli.app, ["turn-off", TEST_ADDRESS])

    assert result.exit_code == 0
    assert device.calls == [("turn_off", (), {})]


@pytest.mark.component
def test_set_brightness_cli_drives_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """The set-brightness command passes all brightness values to the device."""
    device = _patch_device(monkeypatch)

    result = RUNNER.invoke(cli.app, ["set-brightness", TEST_ADDRESS, "60", "80", "100"])

    assert result.exit_code == 0
    assert device.calls == [("set_brightness", ([60, 80, 100],), {})]


@pytest.mark.component
def test_add_setting_cli_drives_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """The add-setting command parses schedule options and calls the device API."""
    device = _patch_device(monkeypatch)

    result = RUNNER.invoke(
        cli.app,
        [
            "add-setting",
            TEST_ADDRESS,
            "08:00",
            "18:30",
            "75",
            "--ramp-up-in-minutes",
            "30",
            "--weekdays",
            "monday",
            "--weekdays",
            "tuesday",
        ],
    )

    assert result.exit_code == 0
    assert device.calls == [
        (
            "add_setting",
            (),
            {
                "sunrise": datetime(1900, 1, 1, 8, 0),
                "sunset": datetime(1900, 1, 1, 18, 30),
                "max_brightness": [75],
                "ramp_up_in_minutes": 30,
                "weekdays": [cli.WeekdaySelect.monday, cli.WeekdaySelect.tuesday],
            },
        )
    ]


@pytest.mark.component
def test_remove_setting_cli_drives_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """The remove-setting command parses schedule options and calls the device API."""
    device = _patch_device(monkeypatch)

    result = RUNNER.invoke(
        cli.app,
        [
            "remove-setting",
            TEST_ADDRESS,
            "08:00",
            "18:30",
            "--ramp-up-in-minutes",
            "30",
            "--weekdays",
            "monday",
        ],
    )

    assert result.exit_code == 0
    assert device.calls == [
        (
            "remove_setting",
            (),
            {
                "sunrise": datetime(1900, 1, 1, 8, 0),
                "sunset": datetime(1900, 1, 1, 18, 30),
                "ramp_up_in_minutes": 30,
                "weekdays": [cli.WeekdaySelect.monday],
            },
        )
    ]


@pytest.mark.component
def test_reset_settings_cli_drives_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reset-settings command calls the device reset_settings API."""
    device = _patch_device(monkeypatch)

    result = RUNNER.invoke(cli.app, ["reset-settings", TEST_ADDRESS])

    assert result.exit_code == 0
    assert device.calls == [("reset_settings", (), {})]


@pytest.mark.component
def test_enable_auto_mode_cli_drives_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """The enable-auto-mode command calls the device enable_auto_mode API."""
    device = _patch_device(monkeypatch)

    result = RUNNER.invoke(cli.app, ["enable-auto-mode", TEST_ADDRESS])

    assert result.exit_code == 0
    assert device.calls == [("enable_auto_mode", (), {})]


@pytest.mark.component
def test_dose_cli_triggers_dosing_pump(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dose command resolves a pump and converts user channel numbers to zero-based indexes."""
    calls: list[tuple[int, float]] = []

    async def get_device_from_address(address: str) -> ChihirosDevice:
        assert address == TEST_ADDRESS
        device = ChihirosDosingPump(FakeBLEDevice(address=TEST_ADDRESS), DOSING_PUMP)  # type: ignore[arg-type]

        async def dose_ml(pump_idx: int, volume_ml: float) -> None:
            calls.append((pump_idx, volume_ml))

        device.dose_ml = dose_ml  # type: ignore[method-assign]
        return device

    monkeypatch.setattr(cli, "get_device_from_address", get_device_from_address)

    result = RUNNER.invoke(cli.app, ["dosing", "dose", TEST_ADDRESS, "2", "2.5"])

    assert result.exit_code == 0
    assert calls == [(1, 2.5)]


@pytest.mark.component
@pytest.mark.parametrize(
    ("options", "expected_mask"),
    [([], None), (["--daily-ml", "1.0"], 127), (["--daily-ml", "1.0", "--weekdays", "monday"], 64)],
)
def test_program_cli_weekday_mask(
    monkeypatch: pytest.MonkeyPatch, options: list[str], expected_mask: int | None
) -> None:
    """The CLI leaves repetition alone without volume and encodes it when supplied."""
    masks: list[int] = []

    async def get_device_from_address(address: str) -> ChihirosDosingPump:
        device = ChihirosDosingPump(FakeBLEDevice(address=address), DOSING_PUMP)  # type: ignore[arg-type]

        async def capture(commands: list[bytes], _: int) -> None:
            masks.extend(frame[7] for frame in commands if (frame[0], frame[5]) == (165, 27))

        device._send_command = capture  # type: ignore[method-assign]
        return device

    monkeypatch.setattr(cli, "get_device_from_address", get_device_from_address)
    result = RUNNER.invoke(cli.app, ["dosing", "program", TEST_ADDRESS, "1", "08:00:1.0", *options])
    assert result.exit_code == 0, result.output
    assert masks == ([] if expected_mask is None else [expected_mask])


@pytest.mark.unit
def test_program_cli_rejects_weekdays_without_daily_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid weekday updates fail before looking for a pump."""

    async def unexpected_lookup(_: str) -> None:
        pytest.fail("Invalid program request should not resolve a pump")

    monkeypatch.setattr(cli, "get_device_from_address", unexpected_lookup)
    result = RUNNER.invoke(cli.app, ["dosing", "program", TEST_ADDRESS, "1", "--weekdays", "monday"])
    assert result.exit_code == 2
    assert "requires --daily-ml" in unstyle(result.output)


@pytest.mark.component
def test_dose_cli_rejects_non_dosing_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dose command fails clearly for light devices."""

    async def get_device_from_address(address: str) -> ChihirosDevice:
        assert address == TEST_ADDRESS
        return ChihirosDevice(
            FakeBLEDevice(address=TEST_ADDRESS), DeviceModel("Test Light", (), LedSpec(WHITE_CHANNELS))
        )  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "get_device_from_address", get_device_from_address)

    result = RUNNER.invoke(cli.app, ["dosing", "dose", TEST_ADDRESS, "1", "1.0"])

    assert result.exit_code != 0
    assert "not a dosing pump" in result.output
