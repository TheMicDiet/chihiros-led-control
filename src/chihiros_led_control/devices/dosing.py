"""Dosing-pump family driver."""

from __future__ import annotations

from collections.abc import Sequence

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from ..models import DeviceModel, DosingPumpSpec
from ..protocol import dosing as commands
from ..protocol.frame import encode_timestamp
from ..protocol.notifications import DosingDailyNotification, DosingTotalsNotification, ParsedNotification
from ..registry import DOSING_PUMP, detect_model
from ..transport import ChihirosTransport
from .base import STATUS_NOTIFICATION_WAIT, BaseChihirosDevice, DeviceClock


class ChihirosDosingPump(BaseChihirosDevice):
    """Concrete BLE client for a Chihiros dosing pump."""

    def __init__(
        self,
        ble_device: BLEDevice,
        model: DeviceModel | None = None,
        advertisement_data: AdvertisementData | None = None,
        *,
        transport: ChihirosTransport | None = None,
        clock: DeviceClock | None = None,
    ) -> None:
        """Create a dosing-pump client."""
        detected = detect_model(ble_device.name)
        if isinstance(detected.spec, DosingPumpSpec) and (model is None or detected.spec.dydose_8):
            model = detected  # A generic override must not allow aliased reset selectors on DYDOSE.
        super().__init__(ble_device, model or DOSING_PUMP, advertisement_data, transport=transport, clock=clock)
        self.last_dosing_totals_notification: DosingTotalsNotification | None = None
        self.last_dosing_daily_notification: DosingDailyNotification | None = None

    @property
    def uses_dydose_8_protocol(self) -> bool:
        """Return whether the legacy pump behavior applies."""
        spec = self.model.spec
        return isinstance(spec, DosingPumpSpec) and spec.dydose_8

    def _encode_device_timestamp(self) -> list[int]:
        return encode_timestamp(self._device_time(), sunday_first=self.uses_dydose_8_protocol)

    def _validate_channel(self, channel: int) -> None:
        spec = self.model.spec
        channel_limit = spec.channel_limit if isinstance(spec, DosingPumpSpec) else 8
        if not 0 <= channel < channel_limit:
            raise ValueError(f"Channel index must be between 0 and {channel_limit - 1} on this pump")

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
        # DYDOSE-8 stores one pending reply selector; let the first query be
        # serviced before sending the next one. The transport keeps the link open.
        batches = commands_to_send if self.uses_dydose_8_protocol else [commands_to_send]
        for batch in batches:
            await self._send_command(batch, 3, notification_wait=STATUS_NOTIFICATION_WAIT)

    async def dose_ml(self, pump_idx: int, volume_ml: float, *, extra: bool = True) -> bytes:
        """Trigger an immediate manual dose on one pump channel.

        Returns the exact ``tempDosingCode`` frame that was sent — the app
        broadcasts this frame verbatim to every connected device when a
        stirrer slave is linked (DOSING_CONTROL.md §5, 0xa62d78).
        """
        self._validate_channel(pump_idx)
        manual_dose_frame = commands.create_manual_dose_command(
            self.get_next_msg_id(), pump_idx, volume_ml, extra=extra and self.uses_dydose_8_protocol
        )
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

        ``frequency`` is the wire weekday mask (127 = every day). On DYDOSE-8
        a false ``is_first_setting`` inhibits scheduled dosing for the day;
        setting it true clears that latch without resetting the counters.
        """
        self._validate_channel(channel)
        cmd = commands.create_dosing_set_command(
            self.get_next_msg_id(), channel, dose_per_day_ml, frequency, is_first_setting=is_first_setting
        )
        await self._send_command(cmd, 3)

    async def set_channel_active(self, channel: int, *, active: bool = True, compensate: bool = False) -> None:
        """Set channel state; on DYDOSE-8 disabling clears the schedule slots."""
        self._validate_channel(channel)
        if active and self.uses_dydose_8_protocol:
            raise ValueError("DYDOSE cannot enable a channel without reprogramming its schedule")
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
        frequency: int | None = None,
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
        half-programmed. Omitting daily volume skips the ``dosingSet`` frame;
        a frequency may only be supplied with daily volume and defaults to
        every day in that case. ``mode=None`` skips the schedule frames.
        DYDOSE-8 only writes schedule slots for active channels. Daily settings
        always clear the inhibit latch. An active daily-settings-only write
        leaves existing slots and compensation untouched.
        """
        self._validate_channel(channel)
        if dose_per_day_ml is None and frequency is not None:
            raise ValueError("A weekday frequency requires a daily volume")
        legacy = self.uses_dydose_8_protocol
        commands_to_send = self._program_state_frames(channel, active=active, compensate=compensate, mode=mode)
        if dose_per_day_ml is not None:
            commands_to_send.append(
                commands.create_dosing_set_command(
                    self.get_next_msg_id(),
                    channel,
                    dose_per_day_ml,
                    127 if frequency is None else frequency,
                    is_first_setting=legacy or is_first_setting,
                )
            )
        if mode is not None:
            schedule_frames = self._schedule_frames(channel, mode, points)
            commands_to_send.extend(schedule_frames if active or not legacy else ())
        if not commands_to_send:
            raise ValueError("DYDOSE requires schedule points to enable a channel, or daily volume to update settings")
        await self._send_command(commands_to_send, 3)

    def _program_state_frames(
        self, channel: int, *, active: bool, compensate: bool, mode: commands.DosingMode | None
    ) -> list[bytearray]:
        if self.uses_dydose_8_protocol and active and mode is None:
            return []  # A daily-settings-only write must not clear the existing slots.
        return [
            commands.create_dosing_active_compensation_command(
                self.get_next_msg_id(), channel, active=active, compensate=compensate
            )
        ]

    def _schedule_frames(
        self, channel: int, mode: commands.DosingMode, points: Sequence[commands.DosingWorkPoint]
    ) -> list[bytearray]:
        if self.uses_dydose_8_protocol:
            commands.validate_dydose_schedule(mode, points)
        return commands.create_dosing_schedule_command(self.get_next_msg_id(), channel, mode, points)

    async def set_schedule(
        self, channel: int, mode: commands.DosingMode, points: Sequence[commands.DosingWorkPoint]
    ) -> None:
        """Replace one channel's schedule with the given work points.

        DYDOSE-8 requires a clear first. This preserves the daily volume and
        weekday mask but disables interrupt compensation; use ``program_channel``
        to explicitly set compensation as part of the replacement.
        """
        self._validate_channel(channel)
        cmds: list[bytearray] = []
        if self.uses_dydose_8_protocol:
            cmds.append(
                commands.create_dosing_active_compensation_command(
                    self.get_next_msg_id(), channel, active=True, compensate=False
                )
            )
        cmds.extend(self._schedule_frames(channel, mode, points))
        await self._send_command(cmds, 3)

    async def reset_channel(self, channel: int) -> bytes:
        """Reset one channel's programming (app's ``resetDosingChannel``).

        Returns the exact frame sent — the app broadcasts it verbatim to
        linked stirrers (DOSING_CONTROL.md §5, 0x927588).
        """
        self._validate_channel(channel)
        cmd = commands.create_reset_dosing_channel_command(self.get_next_msg_id(), channel)
        await self._send_command(cmd, 3)
        return bytes(cmd)

    async def send_frame(self, frame: bytes | bytearray) -> None:
        """Send a pre-built frame verbatim (master/slave broadcast replay).

        The vendor app delivers one ``DataSendEvent``'s frames — byte for
        byte, including message id and checksum — to every connected device
        when a stirrer slave is linked (DOSING_CONTROL.md §5). This is the
        equivalent primitive for replaying such a frame onto another device.

        Opaque frames may trigger physical dosing. Attempt once because a
        failed write can already have been accepted by the device.
        """
        await self._send_command(bytearray(frame), 1)

    async def reset_total_dosed(self, channel: int) -> None:
        """Zero one channel's lifetime dosed counter (app's ``resetTotalDosing``)."""
        self._validate_channel(channel)
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
        self._validate_channel(channel)
        if seconds is None and volume_ml is None:
            raise ValueError("Calibration needs either seconds or volume_ml")
        if self.uses_dydose_8_protocol and seconds not in (None, 5):
            raise ValueError("DYDOSE calibration only supports a fixed 5-second run")
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
