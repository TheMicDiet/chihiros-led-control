"""Chihiros led control CLI entrypoint."""

import asyncio
import inspect
from datetime import datetime
from typing import Any

import typer
from bleak import BleakScanner
from rich import print
from rich.table import Table
from typing_extensions import Annotated

from chihiros_led_control import commands
from chihiros_led_control.device import get_device_from_address
from chihiros_led_control.weekday_encoding import WeekdaySelect

UART_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
UART_RX_CHAR_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
UART_TX_CHAR_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"


app = typer.Typer()

msg_id = commands.next_message_id()


def _run_device_func(device_address: str, **kwargs: Any) -> None:
    command_name = inspect.stack()[1][3]

    async def _async_func() -> None:
        dev = await get_device_from_address(device_address)
        if hasattr(dev, command_name):
            await getattr(dev, command_name)(**kwargs)
        else:
            print(f"{dev.__class__.__name__} doesn't support {command_name}")
            raise typer.Abort()

    asyncio.run(_async_func())


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
    _run_device_func(device_address, brightness=brightness)


@app.command()
def set_rgb_brightness(
    device_address: str, brightness: Annotated[tuple[int, int, int], typer.Argument()]
) -> None:
    _run_device_func(device_address, brightness=brightness)


@app.command()
def add_setting(
    device_address: str,
    sunrise: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    sunset: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    max_brightness: Annotated[int, typer.Option(max=100, min=0)] = 100,
    ramp_up_in_minutes: Annotated[int, typer.Option(min=0, max=150)] = 0,
    weekdays: Annotated[list[WeekdaySelect], typer.Option()] = [WeekdaySelect.everyday],
) -> None:
    _run_device_func(
        device_address,
        sunrise=sunrise,
        sunset=sunset,
        max_brightness=max_brightness,
        ramp_up_in_minutes=ramp_up_in_minutes,
        weekdays=weekdays,
    )


@app.command()
def add_rgb_setting(
    device_address: str,
    sunrise: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    sunset: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    max_brightness: Annotated[tuple[int, int, int], typer.Option()] = (100, 100, 100),
    ramp_up_in_minutes: Annotated[int, typer.Option(min=0, max=150)] = 0,
    weekdays: Annotated[list[WeekdaySelect], typer.Option()] = [WeekdaySelect.everyday],
) -> None:
    _run_device_func(
        device_address,
        sunrise=sunrise,
        sunset=sunset,
        max_brightness=max_brightness,
        ramp_up_in_minutes=ramp_up_in_minutes,
        weekdays=weekdays,
    )


@app.command()
def remove_setting(
    device_address: str,
    sunrise: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    sunset: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    ramp_up_in_minutes: Annotated[int, typer.Option(min=0, max=150)] = 0,
    weekdays: Annotated[list[WeekdaySelect], typer.Option()] = [WeekdaySelect.everyday],
) -> None:
    _run_device_func(
        device_address,
        sunrise=sunrise,
        sunset=sunset,
        ramp_up_in_minutes=ramp_up_in_minutes,
        weekdays=weekdays,
    )


@app.command()
def reset_settings(device_address: str) -> None:
    _run_device_func(device_address)


@app.command()
def enable_auto_mode(device_address: str) -> None:
    _run_device_func(device_address)


if __name__ == "__main__":
    try:
        app()
    except asyncio.CancelledError:
        pass
