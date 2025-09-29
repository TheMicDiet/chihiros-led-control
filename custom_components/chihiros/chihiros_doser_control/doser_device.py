from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from typing import List

import typer
from typing_extensions import Annotated

from ..chihiros_led_control.device.base_device import BaseDevice
from ..chihiros_led_control import commands as led_cmds  # set_time
from ..chihiros_led_control.weekday_encoding import WeekdaySelect, encode_selected_weekdays
from . import dosingpump
from .protocol import _split_ml_25_6, UART_TX

app = typer.Typer(help="Chihiros doser control")


# ─────────────────────────────────────────────────────────────────────
# Device class (no Typer annotations here)
# ─────────────────────────────────────────────────────────────────────

class DoserDevice(BaseDevice):
    """Doser-specific commands mixed onto the common BLE BaseDevice."""

    @staticmethod
    def _add_minutes(t: time, delta_min: int) -> time:
        anchor = datetime(2000, 1, 1, t.hour, t.minute)
        return (anchor + timedelta(minutes=delta_min)).time()

    async def raw_dosing_pump(
        self,
        cmd_id: int,
        mode: int,
        params: List[int] | None = None,
        repeats: int = 3,
    ) -> None:
        """Send a raw A5 frame (165/…) with checksum and msg-id handled."""
        p = params or []
        pkt = dosingpump._create_command_encoding_dosing_pump(  # type: ignore[attr-defined]
            cmd_id, mode, self.get_next_msg_id(), p
        )
        await self._send_command(pkt, repeats)

    async def set_dosing_pump_manuell_ml(self, ch_id: int, ch_ml: float) -> None:
        """Immediate dose on channel using 25.6-bucket + 0.1 remainder (single frame)."""
        hi, lo = _split_ml_25_6(ch_ml)
        cmd = dosingpump.create_add_dosing_pump_command_manuell_ml(
            self.get_next_msg_id(), ch_id, hi, lo
        )
        await self._send_command(cmd, 3)

    async def add_setting_dosing_pump(
        self,
        performance_time: time,
        ch_id: int,
        weekdays_mask: int,
        ch_ml_tenths: int,
    ) -> None:
        """
        Programs one daily dose entry at HH:MM with amount (hi/lo), ensures
        device time is synced and auto mode is active.
        """
        prelude = [
            dosingpump.create_order_confirmation(self.get_next_msg_id(), 90, 4, 1),
            led_cmds.create_set_time_command(self.get_next_msg_id()),
            led_cmds.create_set_time_command(self.get_next_msg_id()),
            dosingpump.create_order_confirmation(self.get_next_msg_id(), 165, 4, 4),
            dosingpump.create_order_confirmation(self.get_next_msg_id(), 165, 4, 5),
            dosingpump.create_switch_to_auto_mode_dosing_pump_command(self.get_next_msg_id(), ch_id),
        ]
        for f in prelude:
            await self._send_command(f, 3)

        # timer-type/time (1 = 24h)
        set_time0 = dosingpump.create_auto_mode_dosing_pump_command_time(
            performance_time, self.get_next_msg_id(), ch_id, timer_type=1
        )
        await self._send_command(set_time0, 3)

        # add the dose entry (tenths → split hi/lo inside helper)
        add = dosingpump.create_add_auto_setting_command_dosing_pump(
            performance_time, self.get_next_msg_id(), ch_id, weekdays_mask, ch_ml_tenths
        )
        await self._send_command(add, 3)

    async def enable_auto_mode_dosing_pump(self, ch_id: int) -> None:
        switch_cmd = dosingpump.create_switch_to_auto_mode_dosing_pump_command(self.get_next_msg_id(), ch_id)
        time_cmd = led_cmds.create_set_time_command(self.get_next_msg_id())
        await self._send_command(switch_cmd, 3)
        await self._send_command(time_cmd, 3)

    # placeholders for later
    async def read_dosing_pump_auto_settings(self, ch_id: int | None = None, timeout_s: float = 2.0) -> None:
        typer.echo("read_dosing_pump_auto_settings: query/parse not implemented yet.")

    async def read_dosing_container_status(self, ch_id: int | None = None, timeout_s: float = 2.0) -> None:
        typer.echo("read_dosing_container_status: query/parse not implemented yet.")


# ─────────────────────────────────────────────────────────────────────
# Typer CLI wrappers (these are what `chihirosctl` calls)
# ─────────────────────────────────────────────────────────────────────

@app.command("set-dosing-pump-manuell-ml")
def cli_set_dosing_pump_manuell_ml(
    device_address: Annotated[str, typer.Argument(help="BLE MAC, e.g. AA:BB:CC:DD:EE:FF")],
    ch_id: Annotated[int, typer.Option("--ch-id", help="Channel 0..3", min=0, max=3)],
    ch_ml: Annotated[float, typer.Option("--ch-ml", help="Dose (mL)", min=0.2, max=999.9)],
):
    """Immediate one-shot dose."""
    dd = DoserDevice(device_address)
    asyncio.run(dd.set_dosing_pump_manuell_ml(ch_id, ch_ml))


@app.command("add-setting-dosing-pump")
def cli_add_setting_dosing_pump(
    device_address: Annotated[str, typer.Argument(help="BLE MAC")],
    performance_time: Annotated[datetime, typer.Argument(formats=["%H:%M"], help="HH:MM")],
    ch_id: Annotated[int, typer.Option("--ch-id", help="Channel 0..3", min=0, max=3)],
    weekdays: Annotated[List[WeekdaySelect], typer.Option(
        "--weekdays", "-w",
        help="Repeat days; can be passed multiple times",
        case_sensitive=False
    )] = [WeekdaySelect.everyday],
    ch_ml: Annotated[float, typer.Option("--ch-ml", help="Daily dose mL", min=0.2, max=999.9)],
):
    """Add a 24h schedule entry at time with amount, on selected weekdays."""
    dd = DoserDevice(device_address)
    mask = encode_selected_weekdays(weekdays)
    tenths = int(round(ch_ml * 10))
    asyncio.run(dd.add_setting_dosing_pump(performance_time.time(), ch_id, mask, tenths))


@app.command("raw-dosing-pump")
def cli_raw_dosing_pump(
    device_address: Annotated[str, typer.Argument(help="BLE MAC")],
    cmd_id: Annotated[int, typer.Option("--cmd-id", help="Command (e.g. 165)")],
    mode: Annotated[int, typer.Option("--mode", help="Mode (e.g. 27)")],
    repeats: Annotated[int, typer.Option("--repeats", help="Send frame N times", min=1)] = 3,
    params: Annotated[List[int], typer.Argument(help="Parameter list, e.g. 0 0 14 2 0 0")] = typer.Argument(...),
):
    """Send a raw A5 frame: [cmd, 1, len, msg_hi, msg_lo, mode, *params, checksum]."""
    dd = DoserDevice(device_address)
    asyncio.run(dd.raw_dosing_pump(cmd_id, mode, params, repeats))


if __name__ == "__main__":
    app()
