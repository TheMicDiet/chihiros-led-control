"""Chihiros LED control CLI entrypoint."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime

import typer
from bleak import BleakScanner
from rich import print
from rich.table import Table
from typing_extensions import Annotated

from .client import ChihirosDevice
from .factory import detect_model, get_device_from_address
from .weekday_encoding import WeekdaySelect

app = typer.Typer()

DeviceCommand = Callable[[ChihirosDevice], Awaitable[None]]


def _run_device_func(device_address: str, command: DeviceCommand) -> None:
    async def _async_func() -> None:
        dev = await get_device_from_address(device_address)
        await command(dev)

    asyncio.run(_async_func())


@app.command()
def list_devices(timeout: Annotated[int, typer.Option()] = 5) -> None:
    """List all bluetooth devices."""
    table = Table("Name", "Address", "Model")
    discovered_devices = asyncio.run(BleakScanner.discover(timeout=timeout))
    for device in discovered_devices:
        model = detect_model(device.name)
        model_name = "???" if model.fallback else model.name
        table.add_row(device.name, device.address, model_name)
    print("Discovered the following devices:")
    print(table)


@app.command()
def turn_on(device_address: str) -> None:
    """Turn on a light."""
    _run_device_func(device_address, lambda dev: dev.turn_on())


@app.command()
def turn_off(device_address: str) -> None:
    """Turn off a light."""
    _run_device_func(device_address, lambda dev: dev.turn_off())


@app.command()
def set_color_brightness(
    device_address: str,
    color: int,
    brightness: Annotated[int, typer.Argument(min=0, max=100)],
) -> None:
    """Set color brightness of a light."""
    _run_device_func(device_address, lambda dev: dev.set_color_brightness(brightness, color))


@app.command()
def set_brightness(device_address: str, brightness: Annotated[int, typer.Argument(min=0, max=100)]) -> None:
    """Set brightness of a light."""
    set_color_brightness(device_address, color=0, brightness=brightness)


@app.command()
def set_rgb_brightness(device_address: str, brightness: Annotated[tuple[int, int, int], typer.Argument()]) -> None:
    """Set brightness of a RGB light."""
    _run_device_func(device_address, lambda dev: dev.set_rgb_brightness(brightness))


@app.command()
def add_setting(
    device_address: str,
    sunrise: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    sunset: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    max_brightness: Annotated[int, typer.Option(max=100, min=0)] = 100,
    ramp_up_in_minutes: Annotated[int, typer.Option(min=0, max=150)] = 0,
    weekdays: Annotated[list[WeekdaySelect], typer.Option()] = [WeekdaySelect.everyday],
) -> None:
    """Add setting to a light."""
    _run_device_func(
        device_address,
        lambda dev: dev.add_setting(
            sunrise=sunrise,
            sunset=sunset,
            max_brightness=max_brightness,
            ramp_up_in_minutes=ramp_up_in_minutes,
            weekdays=weekdays,
        ),
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
    """Add setting to a RGB light."""
    _run_device_func(
        device_address,
        lambda dev: dev.add_rgb_setting(
            sunrise=sunrise,
            sunset=sunset,
            max_brightness=max_brightness,
            ramp_up_in_minutes=ramp_up_in_minutes,
            weekdays=weekdays,
        ),
    )


@app.command()
def remove_setting(
    device_address: str,
    sunrise: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    sunset: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    ramp_up_in_minutes: Annotated[int, typer.Option(min=0, max=150)] = 0,
    weekdays: Annotated[list[WeekdaySelect], typer.Option()] = [WeekdaySelect.everyday],
) -> None:
    """Remove setting from a light."""
    _run_device_func(
        device_address,
        lambda dev: dev.remove_setting(
            sunrise=sunrise,
            sunset=sunset,
            ramp_up_in_minutes=ramp_up_in_minutes,
            weekdays=weekdays,
        ),
    )


@app.command()
def reset_settings(device_address: str) -> None:
    """Reset settings from a light."""
    _run_device_func(device_address, lambda dev: dev.reset_settings())


@app.command()
def enable_auto_mode(device_address: str) -> None:
    """Enable auto mode in a light."""
    _run_device_func(device_address, lambda dev: dev.enable_auto_mode())


if __name__ == "__main__":
    try:
        app()
    except asyncio.CancelledError:
        pass
