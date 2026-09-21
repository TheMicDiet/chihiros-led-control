"""Dosing-pump family driver."""

from __future__ import annotations

from collections.abc import Sequence

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from ..models import DeviceModel
from ..protocol import dosing as commands
from ..protocol.notifications import DosingDailyNotification, DosingTotalsNotification, ParsedNotification
from ..registry import DOSING_PUMP
from ..transport import ChihirosTransport
from .base import STATUS_NOTIFICATION_WAIT, BaseChihirosDevice


class ChihirosDosingPump(BaseChihirosDevice):
    """Concrete BLE client for a Chihiros dosing pump."""

    def __init__(
        self,
        ble_device: BLEDevice,
        model: DeviceModel = DOSING_PUMP,
        advertisement_data: AdvertisementData | None = None,
        *,
        transport: ChihirosTransport | None = None,
    ) -> None:
        """Create a dosing-pump client."""
        super().__init__(ble_device, model, advertisement_data, transport=transport)

    def _parse_notification(self, data: bytes | bytearray) -> ParsedNotification | None:
        """Parse dosing-pump counter notifications."""
        return commands.parse_notification(data)

    def _record_notification(self, parsed: ParsedNotification) -> None:
        """Store the last dosing notification and log its values."""
        if isinstance(parsed, DosingTotalsNotification):
            self.last_dosing_totals_notification = parsed
            self._logger.debug(
                "%s: Dosing totals notification received; total_dosed_ul=%s",
                self.name,
                parsed.total_dosed_ul,
            )
        elif isinstance(parsed, DosingDailyNotification):
            self.last_dosing_daily_notification = parsed
            self._logger.debug(
                "%s: Dosing daily notification received; dose_use_in_day_ul=%s",
                self.name,
                parsed.dose_use_in_day_ul,
            )

    async def query_status(self) -> None:
        """Request lifetime and daily dosing counters in app order."""
        commands_to_send = [
            commands.create_dose_auth_1_command(self.get_next_msg_id()),
            commands.create_dose_auth_2_command(self.get_next_msg_id()),
        ]
        # Counter replies arrive asynchronously after the writes complete, so
        # keep notifications subscribed for the standard status wait.
        await self._send_command(commands_to_send, 3, notification_wait=STATUS_NOTIFICATION_WAIT)

    async def dose_ml(self, pump_idx: int, volume_ml: float) -> bytes:
        """Trigger an immediate manual dose on one pump channel.

        Returns the exact ``tempDosingCode`` frame that was sent — the app
        broadcasts this frame verbatim to every connected device when a
        stirrer slave is linked (DOSING_CONTROL.md §5, 0xa62d78).
        """
        manual_dose_frame = commands.create_manual_dose_command(self.get_next_msg_id(), pump_idx, volume_ml)
        commands_to_send = [
            commands.create_dose_auth_1_command(self.get_next_msg_id()),
            commands.create_dose_auth_2_command(self.get_next_msg_id()),
            manual_dose_frame,
        ]
        # A disconnect after the final write is ambiguous: the pump may already
        # have accepted the dose. Never replay this non-idempotent transaction.
        await self._send_command(commands_to_send, retry=1)
        return bytes(manual_dose_frame)

    async def query_dosed_totals(self) -> None:
        """Request the lifetime-totals readout (app's ``getDosedFromDevice``).

        The reply lands in :attr:`last_dosing_totals_notification` as a
        ``0x5B``/``0x1E`` uplink frame (the app's ``dosing_state_widget``
        compares the smi immediates ``#0x3c`` = ``0x1E``).
        """
        cmd = commands.create_dose_auth_1_command(self.get_next_msg_id())
        await self._send_command(cmd, 3, notification_wait=STATUS_NOTIFICATION_WAIT)

    async def query_dosed_today(self) -> None:
        """Request the dosed-today readout (app's ``getDosedInDayFromDevice``).

        The reply lands in :attr:`last_dosing_daily_notification` as a
        ``0x5B``/``0x22`` uplink frame (the ``#0x44`` smi immediate).
        """
        cmd = commands.create_dose_auth_2_command(self.get_next_msg_id())
        await self._send_command(cmd, 3, notification_wait=STATUS_NOTIFICATION_WAIT)

    async def apply_dosing_settings(
        self,
        channel: int,
        dose_per_day_ml: float | None,
        frequency: int = 127,
        *,
        is_first_setting: bool = True,
    ) -> None:
        """Program a channel's daily dose and weekday repetition (``dosingSet``).

        ``frequency`` is the weekday-repetition bitmask (127 = every day) and
        ``is_first_setting`` tells the pump this is the channel's first
        programming of the day so it resets its counters.
        """
        cmd = commands.create_dosing_set_command(
            self.get_next_msg_id(), channel, dose_per_day_ml, frequency, is_first_setting=is_first_setting
        )
        await self._send_command(cmd, 3)

    async def set_channel_active(self, channel: int, *, active: bool = True, compensate: bool = False) -> None:
        """Enable/disable a channel and its interrupt compensation (``0xA5, 32``).

        The app sends this frame before every settings/schedule write.
        """
        cmd = commands.create_dosing_active_compensation_command(
            self.get_next_msg_id(), channel, active=active, compensate=compensate
        )
        await self._send_command(cmd, 3)

    async def program_channel(
        self,
        channel: int,
        *,
        active: bool,
        compensate: bool = False,
        dose_per_day_ml: float | None = None,
        frequency: int = 127,
        is_first_setting: bool = True,
        mode: commands.DosingMode | None = None,
        points: Sequence[commands.DosingWorkPoint] = (),
    ) -> None:
        """Program one channel in a *single* connection (app's ``startWork``, §5).

        Sends the active/compensation frame, the optional ``dosingSet`` daily
        volume, and the schedule frames as one paced write batch. The frames
        are idempotent and the whole batch is retried on failure, so a retry
        that eventually succeeds converges the channel — but a BLE write batch
        is not atomic: if every retry fails partway, the pump can end up
        half-programmed. ``dose_per_day_ml=None``
        skips the ``dosingSet`` frame; ``mode=None`` skips the schedule frames.
        """
        commands_to_send = [
            commands.create_dosing_active_compensation_command(
                self.get_next_msg_id(), channel, active=active, compensate=compensate
            )
        ]
        if dose_per_day_ml is not None:
            commands_to_send.append(
                commands.create_dosing_set_command(
                    self.get_next_msg_id(), channel, dose_per_day_ml, frequency, is_first_setting=is_first_setting
                )
            )
        if mode is not None:
            commands_to_send.extend(
                commands.create_dosing_schedule_command(self.get_next_msg_id(), channel, mode, points)
            )
        await self._send_command(commands_to_send, 3)

    async def set_schedule(
        self, channel: int, mode: commands.DosingMode, points: Sequence[commands.DosingWorkPoint]
    ) -> None:
        """Replace one channel's schedule with the given work points.

        Sends the (possibly multiple) batched ``dosingWorkNew`` frames in one
        paced transaction; see :func:`commands.create_dosing_schedule_command`.
        """
        cmds = commands.create_dosing_schedule_command(self.get_next_msg_id(), channel, mode, points)
        await self._send_command(cmds, 3)

    async def reset_channel(self, channel: int) -> bytes:
        """Reset one channel's programming (app's ``resetDosingChannel``).

        Returns the exact frame sent — the app broadcasts it verbatim to
        linked stirrers (DOSING_CONTROL.md §5, 0x927588).
        """
        cmd = commands.create_reset_dosing_channel_command(self.get_next_msg_id(), channel)
        await self._send_command(cmd, 3)
        return bytes(cmd)

    async def send_frame(self, frame: bytes | bytearray) -> None:
        """Send a pre-built frame verbatim (master/slave broadcast replay).

        The vendor app delivers one ``DataSendEvent``'s frames — byte for
        byte, including message id and checksum — to every connected device
        when a stirrer slave is linked (DOSING_CONTROL.md §5). This is the
        equivalent primitive for replaying such a frame onto another device.
        """
        await self._send_command(bytearray(frame), 3)

    async def reset_total_dosed(self, channel: int) -> None:
        """Zero one channel's lifetime dosed counter (app's ``resetTotalDosing``)."""
        cmd = commands.create_reset_total_dosing_command(self.get_next_msg_id(), channel)
        await self._send_command(cmd, 3)

    async def calibrate_channel(
        self,
        channel: int,
        *,
        seconds: int | None = None,
        volume_ml: float | None = None,
    ) -> bytes:
        """Run a channel calibration (app's ``dosingCalibrate``).

        Either ``seconds`` (run the pump for a timed test dose) or
        ``volume_ml`` (record the measured volume of a previous test run) must
        be given; the app's wizard sends both variants. Returns the exact
        frame sent — while a stirrer slave is linked the app broadcasts it
        verbatim (DOSING_CONTROL.md §5; the device-null routing is verified
        in ``DosingPumpInfo.calibration`` @ 0xa6922c).
        """
        if seconds is None and volume_ml is None:
            raise ValueError("Calibration needs either seconds or volume_ml")
        cmd = commands.create_dosing_calibrate_command(self.get_next_msg_id(), channel, seconds, volume_ml)
        # A disconnect after the timed-run frame is ambiguous: the pump may
        # already be running the dose. Never replay it (same rule as
        # ``dose_ml``). Recording a measured volume is idempotent, so it keeps
        # the normal retry budget.
        retry = 1 if seconds is not None else 3
        await self._send_command(cmd, retry)
        return bytes(cmd)

    async def set_dose_delay(self, enabled: bool) -> None:
        """Toggle the device-level dose delay flag (app's ``setDosingDelay``)."""
        cmd = commands.create_set_dosing_delay_command(self.get_next_msg_id(), enabled)
        await self._send_command(cmd, 3)


__all__ = ["ChihirosDosingPump"]
