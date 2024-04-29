import typer
from typing_extensions import Annotated

from chihiros_led_control import commands
from chihiros_led_control.device.base_device import BaseDevice


class TinyTerrariumEgg(BaseDevice):
    _model: str = "TinyTerrariumEgg"
    _code: str = "DYDD"

    async def set_rgb_brightness(
        self, brightness: Annotated[tuple[int, int, int], typer.Argument()]
    ) -> None:
        """Set RGB brightness."""
        for c, b in enumerate(brightness):
            cmd = commands.create_manual_setting_command(self.get_next_msg_id(), c, b)
            await self._send_command(cmd, 3)

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

    async def turn_off(self) -> None:
        """Turn off light."""
        cmd = commands.create_manual_setting_command(self.get_next_msg_id(), 0, 0)
        await self._send_command(cmd, 3)
        cmd = commands.create_manual_setting_command(self.get_next_msg_id(), 1, 0)
        await self._send_command(cmd, 3)
