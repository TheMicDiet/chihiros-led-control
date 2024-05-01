"""Tiny Terraform egg device Model."""

import typer
from typing_extensions import Annotated

from chihiros_led_control import commands
from chihiros_led_control.device.base_device import BaseDevice


class TinyTerrariumEgg(BaseDevice):
    """Tiny Terraform egg device Class."""

    _model: str = "TinyTerrariumEgg"
    _code: str = "DYDD"

    async def set_rgb_brightness(
        self, brightness: Annotated[tuple[int, int, int], typer.Argument()]
    ) -> None:
        """Set RGB brightness."""
        # Ignore blue value
        await self.set_red_brightness(brightness[0])
        await self.set_green_brightness(brightness[1])

    async def set_red_brightness(
        self, brightness: Annotated[int, typer.Argument(min=0, max=100)]
    ) -> None:
        """Set red light brightness."""
        cmd = commands.create_manual_setting_command(
            self.get_next_msg_id(), 0, brightness
        )
        await self._send_command(cmd, 3)

    async def set_green_brightness(
        self, brightness: Annotated[int, typer.Argument(min=0, max=100)]
    ) -> None:
        """Set red light brightness."""
        cmd = commands.create_manual_setting_command(
            self.get_next_msg_id(), 1, brightness
        )
        await self._send_command(cmd, 3)

    async def turn_on(self) -> None:
        """Turn on light."""
        await self.set_red_brightness(100)
        await self.set_green_brightness(100)

    async def turn_off(self) -> None:
        """Turn off light."""
        await self.set_red_brightness(0)
        await self.set_green_brightness(0)
