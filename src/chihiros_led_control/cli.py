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

from .client import ChihirosDevice, ChihirosDosingPump, ChihirosMagStirrer
from .commands import DosingMode, DosingWorkPoint, stirrer_dosage_for_minutes
from .factory import detect_model, get_device_from_address
from .weekday_encoding import WeekdaySelect

app = typer.Typer()

DeviceCommand = Callable[[ChihirosDevice], Awaitable[None]]
DosingDeviceCommand = Callable[[ChihirosDosingPump], Awaitable[None]]


def _run_device_func(device_address: str, command: DeviceCommand) -> None:
    async def _async_func() -> None:
        dev = await get_device_from_address(device_address)
        await command(dev)

    asyncio.run(_async_func())


def _run_dosing_func(device_address: str, command: DosingDeviceCommand) -> None:
    """Run a dosing-device command, rejecting non-dosing devices."""

    async def _async_func() -> None:
        dev = await get_device_from_address(device_address)
        if not isinstance(dev, ChihirosDosingPump):
            raise typer.BadParameter(f"{dev.name} is not a dosing pump or stirrer")
        await command(dev)

    asyncio.run(_async_func())


def _parse_clock(value: str) -> tuple[int, int]:
    """Parse an ``HH:MM`` wall-clock time."""
    try:
        hour_text, minute_text = value.split(":")
        hour, minute = int(hour_text), int(minute_text)
    except ValueError as ex:
        raise typer.BadParameter(f"Invalid time {value!r}, expected HH:MM") from ex
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise typer.BadParameter(f"Invalid time {value!r}")
    return hour, minute


def _parse_dose_point(value: str) -> DosingWorkPoint:
    """Parse a schedule point as ``HH:MM:ML`` (dose volume in mL)."""
    try:
        time_text, volume_text = value.rsplit(":", 1)
        hour, minute = _parse_clock(time_text)
        return DosingWorkPoint(hour, minute, volume_ml=float(volume_text))
    except (ValueError, typer.BadParameter) as ex:
        raise typer.BadParameter(f"Invalid dose point {value!r}, expected HH:MM:ML") from ex


def _parse_free_point(value: str) -> DosingWorkPoint:
    """Parse a free-mode point as ``HH:MM-HH:MM:COUNT`` (window + dose count)."""
    try:
        window_text, count_text = value.rsplit(":", 1)
        start_text, end_text = window_text.split("-")
        start_hour, start_minute = _parse_clock(start_text)
        end_hour, end_minute = _parse_clock(end_text)
        start_minutes = start_hour * 60 + start_minute
        end_minutes = end_hour * 60 + end_minute
        if end_minutes <= start_minutes:
            end_minutes += 24 * 60  # windows may wrap past midnight
        count = int(count_text)
        if not 0 <= count <= 255:
            raise typer.BadParameter(f"Dose count must be between 0 and 255, got {count}")
        return DosingWorkPoint(
            start_hour,
            start_minute,
            duration_minutes=end_minutes - start_minutes,
            number=count,
        )
    except (ValueError, typer.BadParameter) as ex:
        raise typer.BadParameter(f"Invalid free-mode point {value!r}, expected HH:MM-HH:MM:COUNT") from ex


def _parse_stir_point(value: str) -> DosingWorkPoint:
    """Parse a stir point as ``HH:MM:MINUTES`` (run time in minutes)."""
    dose_point = _parse_dose_point(value)
    return DosingWorkPoint(
        dose_point.start_hour,
        dose_point.start_minute,
        volume_ml=stirrer_dosage_for_minutes(dose_point.volume_ml),
    )


def _parse_mode(mode: str) -> DosingMode:
    """Resolve a schedule mode name."""
    try:
        return DosingMode[mode.upper()]
    except KeyError as ex:
        raise typer.BadParameter(f"Invalid mode {mode!r}, expected single/auto/free/timer") from ex


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
def set_brightness(device_address: str, brightness: Annotated[list[int], typer.Argument()]) -> None:
    """Set brightness of a light."""
    _run_device_func(device_address, lambda dev: dev.set_brightness(brightness))


@app.command()
def set_fan_speed(device_address: str, speed_percent: int) -> None:
    """Set fan speed percentage on fan-equipped devices."""
    _run_device_func(device_address, lambda dev: dev.set_fan_speed(speed_percent))


@app.command()
def add_setting(
    device_address: str,
    sunrise: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    sunset: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    max_brightness: Annotated[list[int], typer.Argument()],
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
def dose_ml(
    device_address: str,
    pump: Annotated[int, typer.Argument(min=1, max=8)],
    ml: Annotated[float, typer.Argument(min=0.2, max=999.9)],
) -> None:
    """Trigger an immediate manual dose on a dosing pump."""

    async def command(dev: ChihirosDevice) -> None:
        if not isinstance(dev, ChihirosDosingPump):
            raise typer.BadParameter(f"{dev.name} is not a dosing pump")
        await dev.dose_ml(pump - 1, ml)

    _run_device_func(device_address, command)


@app.command()
def doser_schedule(
    device_address: str,
    channel: Annotated[int, typer.Argument(min=1, max=8)],
    points: Annotated[list[str], typer.Argument()],
    mode: Annotated[str, typer.Option(case_sensitive=False)] = "timer",
) -> None:
    """Replace one dosing pump channel's schedule.

    Points are ``HH:MM:ML`` (dose volume) for single/auto/timer mode and
    ``HH:MM-HH:MM:COUNT`` (window and dose count) for free mode.
    """
    dosing_mode = _parse_mode(mode)
    parse_point = _parse_free_point if dosing_mode is DosingMode.FREE else _parse_dose_point
    work_points = [parse_point(point) for point in points]
    _run_dosing_func(device_address, lambda dev: dev.set_schedule(channel - 1, dosing_mode, work_points))


@app.command()
def doser_active(
    device_address: str,
    channel: Annotated[int, typer.Argument(min=1, max=8)],
    enable: Annotated[bool, typer.Option("--enable/--disable")] = True,
    compensate: Annotated[bool, typer.Option("--compensate/--no-compensate")] = False,
) -> None:
    """Enable or disable a dosing pump channel (and interrupt compensation)."""
    _run_dosing_func(
        device_address,
        lambda dev: dev.set_channel_active(channel - 1, active=enable, compensate=compensate),
    )


@app.command()
def doser_daily_dose(
    device_address: str,
    channel: Annotated[int, typer.Argument(min=1, max=8)],
    ml: Annotated[float, typer.Argument(min=0, max=6553.5)],
    frequency: Annotated[int, typer.Option(min=0, max=255)] = 127,
    first_setting: Annotated[bool, typer.Option("--first-setting/--not-first-setting")] = True,
) -> None:
    """Program a channel's daily dose volume and weekday repetition bitmask."""
    _run_dosing_func(
        device_address,
        lambda dev: dev.apply_dosing_settings(channel - 1, ml, frequency, is_first_setting=first_setting),
    )


@app.command()
def doser_reset_channel(device_address: str, channel: Annotated[int, typer.Argument(min=1, max=8)]) -> None:
    """Reset a dosing pump channel's programming."""
    _run_dosing_func(device_address, lambda dev: dev.reset_channel(channel - 1))


@app.command()
def doser_reset_totals(device_address: str, channel: Annotated[int, typer.Argument(min=1, max=8)]) -> None:
    """Zero a dosing pump channel's lifetime dosed counter."""
    _run_dosing_func(device_address, lambda dev: dev.reset_total_dosed(channel - 1))


@app.command()
def doser_calibrate(
    device_address: str,
    channel: Annotated[int, typer.Argument(min=1, max=8)],
    seconds: Annotated[int | None, typer.Option(min=0, max=255)] = None,
    volume: Annotated[float | None, typer.Option(min=0, max=255.99)] = None,
) -> None:
    """Calibrate a dosing pump channel via a timed run or a measured volume."""
    if seconds is None and volume is None:
        raise typer.BadParameter("Provide either --seconds or --volume")
    _run_dosing_func(device_address, lambda dev: dev.calibrate_channel(channel - 1, seconds=seconds, volume_ml=volume))


@app.command()
def doser_delay(
    device_address: str,
    enable: Annotated[bool, typer.Option("--enable/--disable")] = True,
) -> None:
    """Toggle the dosing pump's device-level dose delay flag."""
    _run_dosing_func(device_address, lambda dev: dev.set_dose_delay(enable))


@app.command()
def stir_on(
    device_address: str,
    channel: Annotated[int, typer.Argument(min=1, max=8)],
    seconds: Annotated[int | None, typer.Option(min=0, max=15359)] = None,
) -> None:
    """Manually start one stirrer channel (optionally for a limited time)."""

    async def command(dev: ChihirosDosingPump) -> None:
        if not isinstance(dev, ChihirosMagStirrer):
            raise typer.BadParameter(f"{dev.name} is not a magnetic stirrer")
        await dev.stir(channel - 1, True, seconds=seconds)

    _run_dosing_func(device_address, command)


@app.command()
def stir_off(device_address: str, channel: Annotated[int, typer.Argument(min=1, max=8)]) -> None:
    """Manually stop one stirrer channel."""

    async def command(dev: ChihirosDosingPump) -> None:
        if not isinstance(dev, ChihirosMagStirrer):
            raise typer.BadParameter(f"{dev.name} is not a magnetic stirrer")
        await dev.stir(channel - 1, False)

    _run_dosing_func(device_address, command)


@app.command()
def stir_speed(
    device_address: str,
    channel: Annotated[int, typer.Argument(min=1, max=8)],
    speed: Annotated[int, typer.Argument(min=0, max=100)],
    pre_seconds: Annotated[int, typer.Option(min=0, max=999)] = 0,
) -> None:
    """Set a stirrer channel's speed and pre-stir time (run-advance seconds)."""

    async def command(dev: ChihirosDosingPump) -> None:
        if not isinstance(dev, ChihirosMagStirrer):
            raise typer.BadParameter(f"{dev.name} is not a magnetic stirrer")
        await dev.set_pre_second(channel - 1, pre_seconds, speed)

    _run_dosing_func(device_address, command)


@app.command()
def stir_schedule(
    device_address: str,
    channel: Annotated[int, typer.Argument(min=1, max=8)],
    points: Annotated[list[str], typer.Argument()],
    frequency: Annotated[int, typer.Option(min=0, max=255)] = 127,
    disable: Annotated[bool, typer.Option("--disable")] = False,
) -> None:
    """Replace one stirrer channel's timer schedule.

    Points are ``HH:MM:MINUTES`` where MINUTES is the stir run time; the
    device encodes run minutes with the pump's 0.6 mL/min equivalence.
    Frequency is the weekday repetition bitmask (127 = every day).
    """
    work_points = [_parse_stir_point(point) for point in points]

    async def command(dev: ChihirosDosingPump) -> None:
        if not isinstance(dev, ChihirosMagStirrer):
            raise typer.BadParameter(f"{dev.name} is not a magnetic stirrer")
        await dev.set_stir_schedule(channel - 1, work_points, frequency=frequency, active=not disable)

    _run_dosing_func(device_address, command)


@app.command()
def enable_auto_mode(device_address: str) -> None:
    """Enable auto mode in a light."""
    _run_device_func(device_address, lambda dev: dev.enable_auto_mode())


if __name__ == "__main__":
    try:
        app()
    except asyncio.CancelledError:
        pass
