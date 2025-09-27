from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from typing import List

import typer
from typing_extensions import Annotated

from ..chihiros_led_control.device.base_device import BaseDevice      # your existing BLE plumbing
from ..chihiros_led_control import commands                           # create_set_time_command(...)
from ..chihiros_led_control.weekday_encoding import WeekdaySelect, encode_selected_weekdays
# dosingpump provides frame builders for 165/21, 165/27, 165/32 etc.
from . import dosingpump
# protocol has the canonical ml splitter (25.6 bucket + 0.1 remainder)
from .protocol import _split_ml_25_6


class DoserDevice(BaseDevice):
    """Doser-specific commands mixed onto the common BLE BaseDevice."""

    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _add_minutes(t: time, delta_min: int) -> time:
        anchor = datetime(2000, 1, 1, t.hour, t.minute)
        return (anchor + timedelta(minutes=delta_min)).time()

    # ── raw ──────────────────────────────────────────────────────────────────
    async def raw_dosing_pump(
        self,
        cmd_id: Annotated[int, typer.Option()] = 165,
        mode:   Annotated[int, typer.Option(min=0)] = 27,
        params: Annotated[List[int], typer.Argument(help="Parameter-Liste, z.B. 0 0 14 02 0 0")] | None = None,
        repeats: int = 3,
    ) -> None:
        """Send a raw A5 frame (165/…) with checksum and msg-id handled."""
        p = params or []
        pkt = dosingpump._create_command_encoding_dosing_pump(  # type: ignore[attr-defined]
            cmd_id, mode, self.get_next_msg_id(), p
        )
        await self._send_command(pkt, repeats)

    # ── manual one-shot ──────────────────────────────────────────────────────
    async def set_dosing_pump_manuell_ml(
        self,
        ch_id: Annotated[int, typer.Option(max=3, min=0)] = 0,
        ch_ml: Annotated[float, typer.Option(min=0.2, max=999.9)] = 0.2,
    ) -> None:
        """Immediate dose on channel using 25.6-bucket + 0.1 remainder (single frame)."""
        hi, lo = _split_ml_25_6(ch_ml)          # (hi = floor(ml/25.6), lo = 0.1-ml remainder)
        cmd = dosingpump.create_add_dosing_pump_command_manuell_ml(
            self.get_next_msg_id(), ch_id, hi, lo
        )
        await self._send_command(cmd, 3)

    # ── auto (schedule) ──────────────────────────────────────────────────────
    async def add_setting_dosing_pump(
        self,
        performance_time: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
        ch_id: Annotated[int, typer.Option(max=3, min=0)] = 0,
        weekdays: Annotated[list[WeekdaySelect], typer.Option()] = [WeekdaySelect.everyday],
        # caller gives tenths (e.g., 800 == 80.0 ml) to match your existing CLI
        ch_ml: Annotated[int, typer.Option(min=0, max=9999)] = 0,
    ) -> None:
        """
        Programs one daily dose entry at HH:MM with amount (hi/lo), then ensures
        device time is synced and auto mode is active.
        """
        # “prelude” as in your code/sniffs
        order_a = dosingpump.create_order_confirmation(self.get_next_msg_id(), 90,  4, 1)
        set_time = commands.create_set_time_command(self.get_next_msg_id())
        order_b = dosingpump.create_order_confirmation(self.get_next_msg_id(), 165, 4, 4)
        order_c = dosingpump.create_order_confirmation(self.get_next_msg_id(), 165, 4, 5)
        to_auto = dosingpump.create_switch_to_auto_mode_dosing_pump_command(self.get_next_msg_id(), ch_id)

        # apply prelude
        await self._send_command(order_a, 3)
        await self._send_command(set_time, 3)
        await self._send_command(set_time, 3)
        await self._send_command(order_b, 3)
        await self._send_command(order_c, 3)
        await self._send_command(to_auto, 3)

        # set the timer type/time (1 = 24h)
        set_time0 = dosingpump.create_auto_mode_dosing_pump_command_time(
            performance_time.time(), self.get_next_msg_id(), ch_id, timer_type=1
        )
        await self._send_command(set_time0, 3)

        # add the dose (now encoded as hi/lo; no 25.0 mL bucket splitting needed)
        add = dosingpump.create_add_auto_setting_command_dosing_pump(
            performance_time.time(),
            self.get_next_msg_id(),
            ch_id,
            encode_selected_weekdays(weekdays),
            ch_ml,  # tenths from caller; function converts to hi/lo
        )
        await self._send_command(add, 3)

    async def enable_auto_mode_dosing_pump(
        self,
        ch_id: Annotated[int, typer.Option(max=3, min=0)] = 0,
    ) -> None:
        """Switch to auto mode and update device time once."""
        switch_cmd = dosingpump.create_switch_to_auto_mode_dosing_pump_command(self.get_next_msg_id(), ch_id)
        time_cmd   = commands.create_set_time_command(self.get_next_msg_id())
        await self._send_command(switch_cmd, 3)
        await self._send_command(time_cmd, 3)

    # ── reads (placeholders until query frames/parsers exist) ─────────────────
    async def read_dosing_pump_auto_settings(
        self,
        ch_id: Annotated[int | None, typer.Option()] = None,
        timeout_s: Annotated[float, typer.Option()] = 2.0,
    ) -> None:
        typer.echo("read_dosing_pump_auto_settings: query/parse not implemented yet.")

    async def read_dosing_container_status(
        self,
        ch_id: Annotated[int | None, typer.Option()] = None,
        timeout_s: Annotated[float, typer.Option()] = 2.0,
    ) -> None:
        typer.echo("read_dosing_container_status: query/parse not implemented yet.")
