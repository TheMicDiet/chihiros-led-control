"""Magnetic-stirrer family driver."""

from __future__ import annotations

from collections.abc import Sequence

from ..protocol import dosing, stirrer
from .base import BaseChihirosDevice


class ChihirosMagStirrer(BaseChihirosDevice):
    """Concrete BLE client for a magnetic stirrer."""

    async def send_frame(self, frame: bytes | bytearray) -> None:
        """Send a pre-built frame verbatim (master/slave broadcast replay).

        The vendor app delivers one ``DataSendEvent``'s frames — byte for
        byte, including message id and checksum — to every connected device
        when a stirrer slave is linked (DOSING_CONTROL.md §5). This is the
        equivalent primitive for replaying such a frame onto another device.
        """
        await self._send_command(bytearray(frame), 3)

    async def set_dose_delay(self, enabled: bool) -> None:
        """Toggle the device-level dose delay flag (app's ``setDosingDelay``)."""
        cmd = dosing.create_set_dosing_delay_command(self.get_next_msg_id(), enabled)
        await self._send_command(cmd, 3)

    async def query_status(self) -> None:
        """Keep the stirrer refresh fire-and-forget like the vendor app."""

    async def set_pre_second(
        self,
        channel: int,
        seconds: int,
        speed: int = stirrer.STIRRER_SPEED_DEFAULT,
        *,
        restart: bool = False,
    ) -> None:
        """Set pre-stir time and speed, optionally restarting a running channel."""
        commands_to_send: list[bytes] = []
        if restart:
            commands_to_send.append(dosing.create_general_temp_run_command(self.get_next_msg_id(), {channel: False}))
        commands_to_send.append(
            stirrer.create_stirrer_pre_second_command(self.get_next_msg_id(), channel, seconds, speed)
        )
        if restart:
            commands_to_send.append(dosing.create_general_temp_run_command(self.get_next_msg_id(), {channel: True}))
        await self._send_command(commands_to_send, 3)

    async def stir(self, channel: int, on: bool, *, seconds: int | None = None) -> None:
        """Manually start/stop one stir channel (app's ``tempRun``).

        Other channel bytes stay 255; ``seconds`` bounds the run time and
        defaults to unlimited.
        """
        cmd = dosing.create_general_temp_run_command(self.get_next_msg_id(), {channel: on}, seconds)
        await self._send_command(cmd, 3)

    async def set_stir_schedule(
        self,
        channel: int,
        points: Sequence[dosing.DosingWorkPoint],
        *,
        frequency: int = 127,
        active: bool = True,
        is_first_setting: bool = True,
    ) -> None:
        """Program one stir channel in timer mode (app's ``MagStirrerInfo.startWork``).

        Mirrors the app's sequence: the active/compensation frame, then
        ``dosingSet`` with a daily volume of 0 (the stirrer splits its
        workload from the timer points alone), then — only when the channel
        is active — the timer-mode schedule frames (§6.2 sends
        ``dosingWorkNew`` only ``if is_active != 0``). Point volumes should
        come from :func:`commands.stirrer_dosage_for_minutes`.
        """
        stirrer.validate_stirrer_work_points(points)
        commands_to_send: list[bytes] = [
            dosing.create_dosing_active_compensation_command(
                self.get_next_msg_id(), channel, active=active, compensate=False
            ),
            dosing.create_dosing_set_command(
                self.get_next_msg_id(), channel, 0.0, frequency, is_first_setting=is_first_setting
            ),
        ]
        if active:
            commands_to_send.extend(
                dosing.create_dosing_schedule_command(self.get_next_msg_id(), channel, dosing.DosingMode.TIMER, points)
            )
        await self._send_command(commands_to_send, 3)


__all__ = ["ChihirosMagStirrer"]
