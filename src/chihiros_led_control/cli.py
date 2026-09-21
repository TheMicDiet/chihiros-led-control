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

from .devices import ChihirosDevice, ChihirosDosingPump, ChihirosHeater, ChihirosMagStirrer
from .factory import detect_model, get_device_from_address
from .models import DeviceKind
from .protocol.dosing import (
    DOSE_VOLUME_MAX_ML,
    MANUAL_DOSE_VOLUME_MAX_ML,
    MANUAL_DOSE_VOLUME_MIN_ML,
    DosingMode,
    DosingWorkPoint,
)
from .protocol.heater import HEATER_MAX_POWER_WATTS, HEATER_MAX_TEMPERATURE_C, encode_heater_power_watts
from .protocol.stirrer import stirrer_dosage_for_minutes, validate_stirrer_work_points
from .weekday_encoding import WeekdaySelect, encode_selected_weekdays

app = typer.Typer()
dosing_app = typer.Typer(help="Control a Chihiros dosing pump (DYDOSE).")
stirrer_app = typer.Typer(help="Control a Chihiros magnetic stirrer (DYMIXR).")
heater_app = typer.Typer(help="Control a Chihiros heater (DYHET).")
app.add_typer(dosing_app, name="dosing", rich_help_panel="Dosing & stirring")
app.add_typer(stirrer_app, name="stirrer", rich_help_panel="Dosing & stirring")
app.add_typer(heater_app, name="heater", rich_help_panel="Heater")

DeviceCommand = Callable[[ChihirosDevice], Awaitable[None]]
DosingDeviceCommand = Callable[[ChihirosDosingPump], Awaitable[None]]
StirrerDeviceCommand = Callable[[ChihirosMagStirrer], Awaitable[None]]
HeaterDeviceCommand = Callable[[ChihirosHeater], Awaitable[None]]


def _run_device_func(device_address: str, command: DeviceCommand) -> None:
    """Run an LED-only command, rejecting every other device family."""

    async def _async_func() -> None:
        dev = await get_device_from_address(device_address)
        try:
            if getattr(dev, "device_kind", None) is not DeviceKind.LED:
                raise typer.BadParameter(f"{dev.name} is not a light")
            await command(dev)
        finally:
            await dev.disconnect()

    asyncio.run(_async_func())


def _run_dosing_func(device_address: str, command: DosingDeviceCommand) -> None:
    """Run a pump-only command, rejecting non-dosing devices and stirrers."""

    async def _async_func() -> None:
        dev = await get_device_from_address(device_address)
        try:
            if getattr(dev, "device_kind", None) is not DeviceKind.DOSING_PUMP:
                raise typer.BadParameter(f"{dev.name} is not a dosing pump")
            await command(dev)
        finally:
            await dev.disconnect()

    asyncio.run(_async_func())


def _run_stirrer_func(device_address: str, command: StirrerDeviceCommand) -> None:
    """Run a stirrer-only command, rejecting every other device."""

    async def _async_func() -> None:
        dev = await get_device_from_address(device_address)
        try:
            if getattr(dev, "device_kind", None) is not DeviceKind.MAG_STIRRER:
                raise typer.BadParameter(f"{dev.name} is not a magnetic stirrer")
            await command(dev)
        finally:
            await dev.disconnect()

    asyncio.run(_async_func())


def _run_heater_func(device_address: str, command: HeaterDeviceCommand) -> None:
    """Run a heater-only command, rejecting every other device."""

    async def _async_func() -> None:
        dev = await get_device_from_address(device_address)
        try:
            if getattr(dev, "device_kind", None) is not DeviceKind.HEATER:
                raise typer.BadParameter(f"{dev.name} is not a heater")
            await command(dev)
        finally:
            await dev.disconnect()

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
        volume = float(volume_text)
    except (ValueError, typer.BadParameter) as ex:
        raise typer.BadParameter(f"Invalid dose point {value!r}, expected HH:MM:ML") from ex
    if not 0 <= volume <= DOSE_VOLUME_MAX_ML:
        raise typer.BadParameter(f"Dose volume must be between 0 and {DOSE_VOLUME_MAX_ML} mL, got {volume}")
    return DosingWorkPoint(hour, minute, volume_ml=volume)


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
    """Parse a stir point as ``HH:MM:MINUTES`` (run time in minutes, 1-999)."""
    try:
        time_text, minutes_text = value.rsplit(":", 1)
        hour, minute = _parse_clock(time_text)
        minutes = float(minutes_text)
    except (ValueError, typer.BadParameter) as ex:
        raise typer.BadParameter(f"Invalid stir point {value!r}, expected HH:MM:MINUTES") from ex
    if not 1 <= minutes <= 999:
        raise typer.BadParameter(f"Stir run time must be between 1 and 999 minutes, got {minutes:g}")
    return DosingWorkPoint(hour, minute, volume_ml=stirrer_dosage_for_minutes(minutes))


def _parse_mode(mode: str) -> DosingMode:
    """Resolve a schedule mode name."""
    try:
        return DosingMode[mode.upper()]
    except KeyError as ex:
        raise typer.BadParameter(f"Invalid mode {mode!r}, expected single/auto/free/timer") from ex


def _parse_work_points(points: list[str], dosing_mode: DosingMode) -> list[DosingWorkPoint]:
    """Parse schedule points using the parser that matches ``dosing_mode``."""
    parse_point = _parse_free_point if dosing_mode is DosingMode.FREE else _parse_dose_point
    return [parse_point(point) for point in points]


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
def enable_auto_mode(device_address: str) -> None:
    """Enable auto mode in a light."""
    _run_device_func(device_address, lambda dev: dev.enable_auto_mode())


@dosing_app.command("dose")
def dosing_dose(
    device_address: str,
    channel: Annotated[int, typer.Argument(min=1, max=8)],
    ml: Annotated[float, typer.Argument(min=MANUAL_DOSE_VOLUME_MIN_ML, max=MANUAL_DOSE_VOLUME_MAX_ML)],
) -> None:
    """Trigger an immediate manual dose on one pump channel."""
    _run_dosing_func(device_address, lambda dev: dev.dose_ml(channel - 1, ml))


@dosing_app.command("schedule")
def dosing_schedule(
    device_address: str,
    channel: Annotated[int, typer.Argument(min=1, max=8)],
    points: Annotated[list[str], typer.Argument()],
    mode: Annotated[str, typer.Option(case_sensitive=False)] = "timer",
) -> None:
    """Replace one pump channel's schedule.

    Points are ``HH:MM:ML`` (dose volume) for single/auto/timer mode and
    ``HH:MM-HH:MM:COUNT`` (window and dose count) for free mode.
    """
    dosing_mode = _parse_mode(mode)
    work_points = _parse_work_points(points, dosing_mode)
    _run_dosing_func(device_address, lambda dev: dev.set_schedule(channel - 1, dosing_mode, work_points))


@dosing_app.command("program")
def dosing_program(
    device_address: str,
    channel: Annotated[int, typer.Argument(min=1, max=8)],
    points: Annotated[list[str] | None, typer.Argument()] = None,
    mode: Annotated[str, typer.Option(case_sensitive=False)] = "timer",
    daily_ml: Annotated[float | None, typer.Option(min=0, max=DOSE_VOLUME_MAX_ML)] = None,
    weekdays: Annotated[list[WeekdaySelect], typer.Option()] = [WeekdaySelect.everyday],
    enable: Annotated[bool, typer.Option("--enable/--disable")] = True,
    compensate: Annotated[bool, typer.Option("--compensate/--no-compensate")] = False,
    first_setting: Annotated[bool, typer.Option("--first-setting/--not-first-setting")] = True,
) -> None:
    """Program a pump channel in a single connection.

    Sends the active/compensation frame, an optional ``--daily-ml`` volume,
    and an optional schedule as one paced transaction (the app's ``startWork``).
    Omit the points to program only the channel state and daily volume.
    """
    dosing_mode = _parse_mode(mode) if points else None
    work_points = _parse_work_points(points, dosing_mode) if dosing_mode is not None else []
    _run_dosing_func(
        device_address,
        lambda dev: dev.program_channel(
            channel - 1,
            active=enable,
            compensate=compensate,
            dose_per_day_ml=daily_ml,
            frequency=encode_selected_weekdays(weekdays),
            is_first_setting=first_setting,
            mode=dosing_mode,
            points=work_points,
        ),
    )


@dosing_app.command("active")
def dosing_active(
    device_address: str,
    channel: Annotated[int, typer.Argument(min=1, max=8)],
    enable: Annotated[bool, typer.Option("--enable/--disable")] = True,
    compensate: Annotated[bool, typer.Option("--compensate/--no-compensate")] = False,
) -> None:
    """Enable or disable a pump channel (and interrupt compensation)."""
    _run_dosing_func(
        device_address,
        lambda dev: dev.set_channel_active(channel - 1, active=enable, compensate=compensate),
    )


@dosing_app.command("calibrate")
def dosing_calibrate(
    device_address: str,
    channel: Annotated[int, typer.Argument(min=1, max=8)],
    seconds: Annotated[int | None, typer.Option(min=0, max=254, help="Timed test dose in seconds (0-254).")] = None,
    volume: Annotated[float | None, typer.Option(min=0, max=255.99)] = None,
) -> None:
    """Calibrate a pump channel via a timed run or a measured volume."""
    if seconds is None and volume is None:
        raise typer.BadParameter("Provide either --seconds or --volume")
    _run_dosing_func(device_address, lambda dev: dev.calibrate_channel(channel - 1, seconds=seconds, volume_ml=volume))


@dosing_app.command("reset")
def dosing_reset(
    device_address: str,
    channel: Annotated[int, typer.Argument(min=1, max=8)],
    totals: Annotated[bool, typer.Option("--totals", help="Zero the lifetime dosed counter instead.")] = False,
) -> None:
    """Reset a pump channel's programming, or its lifetime total with --totals."""
    if totals:
        _run_dosing_func(device_address, lambda dev: dev.reset_total_dosed(channel - 1))
    else:
        _run_dosing_func(device_address, lambda dev: dev.reset_channel(channel - 1))


@dosing_app.command("delay")
def dosing_delay(
    device_address: str,
    enable: Annotated[bool, typer.Option("--enable/--disable")] = True,
) -> None:
    """Toggle the pump's device-level dose delay flag."""
    _run_dosing_func(device_address, lambda dev: dev.set_dose_delay(enable))


@dosing_app.command("totals")
def dosing_totals(device_address: str) -> None:
    """Query and print a pump's lifetime dosed volumes."""

    async def command(dev: ChihirosDosingPump) -> None:
        await dev.query_dosed_totals()
        notification = dev.last_dosing_totals_notification
        if notification is None:
            raise typer.BadParameter(f"{dev.name} did not report lifetime totals")
        table = Table("Channel", "Total (mL)")
        for channel, micro_liters in enumerate(notification.total_dosed_ul, start=1):
            table.add_row(str(channel), f"{micro_liters / 1000:.1f}")
        print(f"Lifetime dosed volumes for {dev.name}:")
        print(table)

    _run_dosing_func(device_address, command)


@dosing_app.command("today")
def dosing_today(device_address: str) -> None:
    """Query and print a pump's volumes dosed today."""

    async def command(dev: ChihirosDosingPump) -> None:
        await dev.query_dosed_today()
        notification = dev.last_dosing_daily_notification
        if notification is None:
            raise typer.BadParameter(f"{dev.name} did not report today's volumes")
        table = Table("Channel", "Today (mL)")
        for channel, micro_liters in enumerate(notification.dose_use_in_day_ul, start=1):
            table.add_row(str(channel), f"{micro_liters / 1000:.1f}")
        print(f"Volumes dosed today for {dev.name}:")
        print(table)

    _run_dosing_func(device_address, command)


@stirrer_app.command("on")
def stirrer_on(
    device_address: str,
    channel: Annotated[int, typer.Argument(min=1, max=8)],
    seconds: Annotated[int | None, typer.Option(min=0, max=15359)] = None,
) -> None:
    """Manually start one stirrer channel (optionally for a limited time)."""
    _run_stirrer_func(device_address, lambda dev: dev.stir(channel - 1, True, seconds=seconds))


@stirrer_app.command("off")
def stirrer_off(
    device_address: str,
    channel: Annotated[int, typer.Argument(min=1, max=8)],
) -> None:
    """Manually stop one stirrer channel."""
    _run_stirrer_func(device_address, lambda dev: dev.stir(channel - 1, False))


@stirrer_app.command("speed")
def stirrer_speed(
    device_address: str,
    channel: Annotated[int, typer.Argument(min=1, max=8)],
    speed: Annotated[int, typer.Argument(min=0, max=100)],
    pre_seconds: Annotated[int, typer.Option(min=0, max=999)] = 0,
    restart: Annotated[bool, typer.Option("--restart/--no-restart")] = False,
) -> None:
    """Set a stirrer channel's speed and pre-stir time."""

    async def command(dev: ChihirosMagStirrer) -> None:
        if restart:
            await dev.set_pre_second(channel - 1, pre_seconds, speed, restart=True)
        else:
            await dev.set_pre_second(channel - 1, pre_seconds, speed)

    _run_stirrer_func(device_address, command)


@stirrer_app.command("schedule")
def stirrer_schedule(
    device_address: str,
    channel: Annotated[int, typer.Argument(min=1, max=8)],
    points: Annotated[list[str], typer.Argument()],
    weekdays: Annotated[list[WeekdaySelect], typer.Option()] = [WeekdaySelect.everyday],
    disable: Annotated[bool, typer.Option("--disable")] = False,
) -> None:
    """Replace one stirrer channel's timer schedule.

    Points are ``HH:MM:MINUTES`` where MINUTES is the stir run time; the
    device encodes run minutes with the pump's 0.6 mL/min equivalence.
    Weekdays select the repetition bitmask.
    """
    work_points = [_parse_stir_point(point) for point in points]
    try:
        validate_stirrer_work_points(work_points)
    except ValueError as ex:
        raise typer.BadParameter(str(ex)) from ex
    frequency = encode_selected_weekdays(weekdays)
    _run_stirrer_func(
        device_address,
        lambda dev: dev.set_stir_schedule(channel - 1, work_points, frequency=frequency, active=not disable),
    )


def _parse_temperature_unit(value: str) -> bool:
    """Parse a heater display unit as Celsius (``c``) or Fahrenheit (``f``)."""
    unit = value.lower()
    if unit in {"c", "celsius"}:
        return True
    if unit in {"f", "fahrenheit"}:
        return False
    raise typer.BadParameter(f"Invalid unit {value!r}, expected c or f")


TemperatureArgument = Annotated[float, typer.Argument(min=0, max=HEATER_MAX_TEMPERATURE_C)]
PowerArgument = Annotated[int, typer.Argument(min=0, max=HEATER_MAX_POWER_WATTS)]


def _validate_heater_power(watts: int) -> int:
    """Validate that heater power can be represented exactly on the wire."""
    try:
        encode_heater_power_watts(watts)
    except ValueError as ex:
        raise typer.BadParameter(str(ex)) from ex
    return watts


@heater_app.command("manual-set")
def heater_manual_set(
    device_address: str,
    temperature: TemperatureArgument,
    watts: PowerArgument,
) -> None:
    """Atomically set a heater's manual temperature and power."""
    _validate_heater_power(watts)
    _run_heater_func(device_address, lambda dev: dev.set_manual_state(temperature, watts))


@heater_app.command("auto-defaults")
def heater_auto_defaults(
    device_address: str,
    temperature: TemperatureArgument,
    watts: PowerArgument,
) -> None:
    """Set the temperature and power the heater's auto schedules heat towards."""
    _validate_heater_power(watts)
    _run_heater_func(device_address, lambda dev: dev.set_auto_defaults(temperature, watts))


@heater_app.command("mode")
def heater_mode(
    device_address: str,
    mode: Annotated[str, typer.Argument(help="manual, auto or scene")],
) -> None:
    """Switch a heater to manual mode, auto mode, or apply its stored scene."""
    heater_commands: dict[str, HeaterDeviceCommand] = {
        "manual": lambda dev: dev.set_manual_mode(),
        "auto": lambda dev: dev.set_auto_mode(),
        "scene": lambda dev: dev.apply_scene(),
    }
    command = heater_commands.get(mode.lower())
    if command is None:
        raise typer.BadParameter(f"Invalid mode {mode!r}, expected manual, auto or scene")
    _run_heater_func(device_address, command)


@heater_app.command("auto-heating")
def heater_auto_heating(
    device_address: str,
    enable: Annotated[bool, typer.Option("--enable/--disable")] = True,
) -> None:
    """Enable or disable the heating element while the heater runs in auto mode."""
    _run_heater_func(device_address, lambda dev: dev.set_auto_heating(enable))


@heater_app.command("backlight")
def heater_backlight(
    device_address: str,
    enable: Annotated[bool, typer.Option("--enable/--disable")] = True,
) -> None:
    """Turn a heater's display backlight on or off."""
    _run_heater_func(device_address, lambda dev: dev.set_backlight(enable))


@heater_app.command("unit")
def heater_unit(
    device_address: str,
    unit: Annotated[str, typer.Argument(help="c or f")],
) -> None:
    """Set the unit a heater displays (°C or °F)."""
    celsius = _parse_temperature_unit(unit)
    _run_heater_func(device_address, lambda dev: dev.set_temperature_unit(celsius=celsius))


@heater_app.command("protector")
def heater_protector(device_address: str, temperature: TemperatureArgument) -> None:
    """Set a heater's overheat protection temperature in °C."""
    _run_heater_func(device_address, lambda dev: dev.set_protector_temperature(temperature))


@heater_app.command("calibrate")
def heater_calibrate(device_address: str, temperature: TemperatureArgument) -> None:
    """Calibrate a heater's sensor against the measured reference temperature."""
    _run_heater_func(device_address, lambda dev: dev.calibrate(temperature))


@heater_app.command("reset-work-time")
def heater_reset_work_time(device_address: str) -> None:
    """Zero a heater's runtime counter after cleaning the heating tube."""
    _run_heater_func(device_address, lambda dev: dev.reset_work_time())


@heater_app.command("status")
def heater_status(device_address: str) -> None:
    """Query and print a heater's temperatures, runtime and alarms."""

    async def command(dev: ChihirosHeater) -> None:
        await dev.query_status()
        temperature = dev.last_heater_temperature_notification
        status = dev.last_heater_status_notification
        table = Table("Field", "Value")
        table.add_row(
            "Setting temperature",
            f"{temperature.setting_temperature_celsius:.1f} °C" if temperature is not None else "unknown",
        )
        table.add_row(
            "Current temperature",
            f"{temperature.current_temperature_celsius:.1f} °C" if temperature is not None else "unknown",
        )
        table.add_row("Runtime", f"{status.work_time_hours} h" if status is not None else "unknown")
        alarms = dev.heater_alarms
        table.add_row("Alarms", ", ".join(alarms) if alarms else ("none" if status is not None else "unknown"))
        print(f"Status for {dev.name}:")
        print(table)

    _run_heater_func(device_address, command)


if __name__ == "__main__":
    try:
        app()
    except asyncio.CancelledError:
        pass
