"""Chihiros led control CLI entrypoint."""

import asyncio
from datetime import datetime
from typing import List

import typer
from bleak import BleakScanner
from rich import print
from rich.table import Table
from typing_extensions import Annotated

from chihiros_led_control import commands
from chihiros_led_control.device.fallback import Fallback
from chihiros_led_control.weekday_encoding import WeekdaySelect

UART_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
UART_RX_CHAR_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
UART_TX_CHAR_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"


app = typer.Typer()

msg_id = commands.next_message_id()


@app.command()
def list_devices(timeout: Annotated[int, typer.Option()] = 5) -> None:
    table = Table("Name", "Address")
    discovered_devices = asyncio.run(BleakScanner.discover(timeout=timeout))
    for device in discovered_devices:
        table.add_row(device.name, device.address)
    print("Discovered the following devices:")
    print(table)


@app.command()
def set_brightness(
    device_address: str, brightness: Annotated[int, typer.Argument(min=0, max=100)]
) -> None:
    async def _set_brightness() -> None:
        ble_dev = await BleakScanner.find_device_by_address(  # type: ignore
            device_address, macos=dict(use_bdaddr=True)
        )
        if ble_dev:
            dev = Fallback(ble_dev)
            await dev.set_brightness(brightness)

    asyncio.run(_set_brightness())


@app.command()
def set_rgb_brightness(
    device_address: str, brightness: Annotated[tuple[int, int, int], typer.Argument()]
) -> None:
    async def _set_rgb_brightness() -> None:
        ble_dev = await BleakScanner.find_device_by_address(  # type: ignore
            device_address, macos=dict(use_bdaddr=True)
        )
        if ble_dev:
            dev = Fallback(ble_dev)
            await dev.set_rgb_brightness(brightness)

    asyncio.run(_set_rgb_brightness())


@app.command()
def add_setting(
    device_address: str,
    sunrise: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    sunset: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    max_brightness: Annotated[int, typer.Option(max=100, min=0)] = 100,
    ramp_up_in_minutes: Annotated[int, typer.Option(min=0, max=150)] = 0,
    weekdays: Annotated[List[WeekdaySelect], typer.Option()] = [WeekdaySelect.everyday],
) -> None:
    async def _add_setting() -> None:
        ble_dev = await BleakScanner.find_device_by_address(  # type: ignore
            device_address, macos=dict(use_bdaddr=True)
        )
        if ble_dev:
            dev = Fallback(ble_dev)
            await dev.add_setting(
                sunrise, sunset, max_brightness, ramp_up_in_minutes, weekdays
            )

    asyncio.run(_add_setting())


@app.command()
def add_rgb_setting(
    device_address: str,
    sunrise: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    sunset: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    max_brightness: Annotated[tuple[int, int, int], typer.Option()] = (100, 100, 100),
    ramp_up_in_minutes: Annotated[int, typer.Option(min=0, max=150)] = 0,
    weekdays: Annotated[List[WeekdaySelect], typer.Option()] = [WeekdaySelect.everyday],
) -> None:
    async def _add_rgb_setting() -> None:
        ble_dev = await BleakScanner.find_device_by_address(  # type: ignore
            device_address, macos=dict(use_bdaddr=True)
        )
        if ble_dev:
            dev = Fallback(ble_dev)
            await dev.add_rgb_setting(
                sunrise, sunset, max_brightness, ramp_up_in_minutes, weekdays
            )

    asyncio.run(_add_rgb_setting())


@app.command()
def remove_setting(
    device_address: str,
    sunrise: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    sunset: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    ramp_up_in_minutes: Annotated[int, typer.Option(min=0, max=150)] = 0,
    weekdays: Annotated[List[WeekdaySelect], typer.Option()] = [WeekdaySelect.everyday],
) -> None:
    async def _remove_setting() -> None:
        ble_dev = await BleakScanner.find_device_by_address(  # type: ignore
            device_address, macos=dict(use_bdaddr=True)
        )
        if ble_dev:
            dev = Fallback(ble_dev)
            await dev.remove_setting(sunrise, sunset, ramp_up_in_minutes, weekdays)

    asyncio.run(_remove_setting())


@app.command()
def reset_settings(device_address: str) -> None:
    async def _reset_setting() -> None:
        ble_dev = await BleakScanner.find_device_by_address(  # type: ignore
            device_address, macos=dict(use_bdaddr=True)
        )
        if ble_dev:
            dev = Fallback(ble_dev)
            await dev.reset_settings()

    asyncio.run(_reset_setting())


@app.command()
def enable_auto_mode(device_address: str) -> None:
    async def _enable_auto_mode() -> None:
        ble_dev = await BleakScanner.find_device_by_address(  # type: ignore
            device_address, macos=dict(use_bdaddr=True)
        )
        if ble_dev:
            dev = Fallback(ble_dev)
            print("Enabling auto mode")
            await dev.enable_auto_mode()

    asyncio.run(_enable_auto_mode())


if __name__ == "__main__":
    try:
        app()
    except asyncio.CancelledError:
        pass
