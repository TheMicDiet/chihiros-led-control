from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from typing import List

import typer
from typing_extensions import Annotated
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDeviceNotFoundError, BleakError

# 👈 go up to the common LED package (shared BaseDevice, time sync, weekday utils)
from ...chihiros_led_control.device.base_device import BaseDevice
from ...chihiros_led_control import commands as led_cmds
from ...chihiros_led_control.weekday_encoding import (
    WeekdaySelect,
    encode_selected_weekdays,
)

app = typer.Typer(help="Chihiros ch4 control")

def _max_rgb_check(brightness):
    sum_rgb = brightness[0] + brightness[1] + brightness[3] + brightness[3]
    if sum_rgb > 400:
        raise ValueError("The values of RGB (red + green + blue + white) must not exceed 400% please correct")



# ─────────────────────────────────────────────────────────────────────
# Device class
# ─────────────────────────────────────────────────────────────────────
class Ch4Device(BaseDevice):
    _model_name = "Universal WRGB"
    _model_codes = [
        "DYU550",
        "DYU600",
        "DYU700",
        "DYU800",
        "DYU920",
        "DYU1000",
        "DYU1200",
        "DYU1500",
    ]
    _colors: dict[str, int] = {
        "red": 0,
        "green": 1,
        "blue": 2,
        "white": 3,
    }
    def __init__(
        self, 
        device_or_addr: BLEDevice | str,
        ) -> None:
        # BaseDevice handles BLEDevice vs string internally
        super().__init__(device_or_addr)
       
    async def set_color_brightness_ch_4(
        self,
        brightness: Annotated[int, typer.Argument(min=0, max=140)],
        color: str | int = 0,
    ) -> None:
        
        """Set brightness of a color."""
        color_id: int | None = None
        if isinstance(color, int) and color in self._colors.values():
            color_id = color
        elif isinstance(color, str) and color in self._colors:
            color_id = self._colors.get(color)
        if color_id is None:
            self._logger.warning("Color not supported: `%s`", color)
            return
        cmd = led_cmds.create_manual_setting_command(
            self.get_next_msg_id(), color_id, brightness
        )
        await self._send_command(cmd, 3)

    async def set_rgb_brightness_ch_4(
        self, 
        brightness: Annotated[tuple[int, int, int, int], typer.Argument()]
    ) -> None:
        """Set RGB brightness."""
        start_cmd   = led_cmds.create_order_confirmation(self.get_next_msg_id(), 90, 4, 1)
        manuell_cmd = led_cmds.create_switch_to_manuell_mode_command(self.get_next_msg_id())
        time_cmd    = led_cmds.create_set_time_command(self.get_next_msg_id())
        await self._send_command(start_cmd, 3)
        await self._send_command(time_cmd, 3)
        await self._send_command(time_cmd, 3)
        await self._send_command(manuell_cmd, 3)
        
        for c, b in enumerate(brightness):
            await self.set_color_brightness_ch_4(b, c)   

# ─────────────────────────────────────────────────────────────────────
# Typer CLI wrappers (scan for device, then operate)
# ─────────────────────────────────────────────────────────────────────
NOT_FOUND_MSG = "Device Not Found, Unreachable or Failed to Connect, ensure Chihiro's App is not connected"


async def _resolve_ble_or_fail(device_address: str) -> BLEDevice:
    ble = await BleakScanner.find_device_by_address(device_address, timeout=12.0)
    if not ble:
        typer.echo(NOT_FOUND_MSG)
        raise typer.Exit(1)
    return ble

def _handle_connect_errors(ex: Exception) -> None:
    # Normalize all "not found / unreachable" style errors to the user-friendly message
    msg = str(ex).lower()
    if (
        isinstance(ex, BleakDeviceNotFoundError)
        or "not found" in msg
        or "unreachable" in msg
        or "failed to connect" in msg
    ):
        typer.echo(NOT_FOUND_MSG)
        raise typer.Exit(1)
    # Otherwise re-raise to show the real error
    raise ex
    
@app.command("set-rgb-brightness-ch-4")
def cli_set_rgb_brightness_ch_4(
    device_address: Annotated[str, typer.Argument(help="BLE MAC, e.g. AA:BB:CC:DD:EE:FF")],
    brightness: Annotated[tuple[int, int, int, int], typer.Argument()]
):
    
    """Immediate one-shot ch4."""
    _max_rgb_check(brightness)
    async def run():
        ch4: Ch4Device | None = None
        try:
            ble = await _resolve_ble_or_fail(device_address)
            print(ble)
            ch4 = Ch4Device(ble)
            await ch4.set_rgb_brightness_ch_4(brightness)
        except (BleakDeviceNotFoundError, BleakError, OSError) as ex:
            _handle_connect_errors(ex)
        finally:
            if ch4:
                await ch4.disconnect()
    asyncio.run(run())

if __name__ == "__main__":
    app()    
