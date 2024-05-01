"""Module defining fallback device."""

from datetime import datetime

import typer
from typing_extensions import Annotated

from .. import commands
from ..weekday_encoding import WeekdaySelect, encode_selected_weekdays
from .base_device import BaseDevice


class Fallback(BaseDevice):
    """Fallback device used when a device is not completely supported yet."""

    _model = "fallback"

    async def set_brightness(
        self, brightness: Annotated[int, typer.Argument(min=0, max=100)]
    ) -> None:
        """Set light brightness."""
        cmd = commands.create_manual_setting_command(
            self.get_next_msg_id(), 0, brightness
        )
        await self._send_command(cmd, 3)

    async def set_rgb_brightness(
        self, brightness: Annotated[tuple[int, int, int], typer.Argument()]
    ) -> None:
        """Set RGB brightness."""
        for c, b in enumerate(brightness):
            cmd = commands.create_manual_setting_command(self.get_next_msg_id(), c, b)
            await self._send_command(cmd, 3)

    async def add_setting(
        self,
        sunrise: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
        sunset: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
        max_brightness: Annotated[int, typer.Option(max=100, min=0)] = 100,
        ramp_up_in_minutes: Annotated[int, typer.Option(min=0, max=150)] = 0,
        weekdays: Annotated[list[WeekdaySelect], typer.Option()] = [
            WeekdaySelect.everyday
        ],
    ) -> None:
        """Add an automation setting to the light."""
        cmd = commands.create_add_auto_setting_command(
            self.get_next_msg_id(),
            sunrise.time(),
            sunset.time(),
            (max_brightness, 255, 255),
            ramp_up_in_minutes,
            encode_selected_weekdays(weekdays),
        )
        await self._send_command(cmd, 3)

    async def add_rgb_setting(
        self,
        sunrise: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
        sunset: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
        max_brightness: Annotated[tuple[int, int, int], typer.Option()] = (
            100,
            100,
            100,
        ),
        ramp_up_in_minutes: Annotated[int, typer.Option(min=0, max=150)] = 0,
        weekdays: Annotated[list[WeekdaySelect], typer.Option()] = [
            WeekdaySelect.everyday
        ],
    ) -> None:
        """Add an automation setting to the RGB light."""
        cmd = commands.create_add_auto_setting_command(
            self.get_next_msg_id(),
            sunrise.time(),
            sunset.time(),
            max_brightness,
            ramp_up_in_minutes,
            encode_selected_weekdays(weekdays),
        )
        await self._send_command(cmd, 3)

    async def remove_setting(
        self,
        sunrise: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
        sunset: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
        ramp_up_in_minutes: Annotated[int, typer.Option(min=0, max=150)] = 0,
        weekdays: Annotated[list[WeekdaySelect], typer.Option()] = [
            WeekdaySelect.everyday
        ],
    ) -> None:
        """Remove an automation setting from the light."""
        cmd = commands.create_delete_auto_setting_command(
            self.get_next_msg_id(),
            sunrise.time(),
            sunset.time(),
            ramp_up_in_minutes,
            encode_selected_weekdays(weekdays),
        )
        await self._send_command(cmd, 3)

    async def reset_settings(self) -> None:
        """Remove all automation settings from the light."""
        cmd = commands.create_reset_auto_settings_command(self.get_next_msg_id())
        await self._send_command(cmd, 3)

    async def enable_auto_mode(self) -> None:
        """Enable auto mode of the light."""
        switch_cmd = commands.create_switch_to_auto_mode_command(self.get_next_msg_id())
        time_cmd = commands.create_set_time_command(self.get_next_msg_id())
        await self._send_command(switch_cmd, 3)
        await self._send_command(time_cmd, 3)

    async def turn_off(self) -> None:
        """Turn off the light."""
        await self.set_rgb_brightness((0, 0, 0))
