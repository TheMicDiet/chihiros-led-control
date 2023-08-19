from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak import BleakClient, BleakScanner
from datetime import datetime
import asyncio
import commands
import typer
from typing_extensions import Annotated
from rich import print
from rich.table import Table
from typing import List
from weekday_encoding import WeekdaySelect, encode_selected_weekdays

UART_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
UART_RX_CHAR_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
UART_TX_CHAR_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"


app = typer.Typer()

msg_id = commands.next_message_id()

@app.command()
def list_devices(timeout: Annotated[int, typer.Option()] = 5):
    table = Table("Name", "Address")    
    discovered_devices = asyncio.run(BleakScanner.discover(timeout))
    for device in discovered_devices:
        table.add_row(device.name, device.address)
    print("Discovered the following devices:")
    print(table)

@app.command()
def set_brightness(device_address: str, brightness: Annotated[int, typer.Argument(min=0, max=100)]):
    cmd = commands.create_manual_setting_command(msg_id, brightness)
    asyncio.run(_execute_command(device_address, cmd))

@app.command()
def add_setting(device_address: str, 
                     sunrise: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
                     sunset: Annotated[datetime, typer.Argument(formats=["%H:%M"])], 
                     max_brightness: Annotated[int, typer.Option(max=100, min=0)] = 100,
                     ramp_up_in_minutes: Annotated[int, typer.Option(min=0, max=150)] = 0, 
                     weekdays: Annotated[List[WeekdaySelect], typer.Option()] = [WeekdaySelect.everyday]):    
    cmd = commands.create_add_auto_setting_command(msg_id, sunrise.time(), sunset.time(), max_brightness, ramp_up_in_minutes, encode_selected_weekdays(weekdays))
    asyncio.run(_execute_command(device_address, cmd))

@app.command()
def remove_setting(device_address: str,
                        sunrise: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
                        sunset: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
                        ramp_up_in_minutes: Annotated[int, typer.Option(
                            min=0, max=150)] = 0,
                        weekdays: Annotated[List[WeekdaySelect], typer.Option()] = [WeekdaySelect.everyday]):
    cmd = commands.create_delete_auto_setting_command(msg_id, sunrise.time(
    ), sunset.time(), ramp_up_in_minutes, encode_selected_weekdays(weekdays))
    asyncio.run(_execute_command(device_address, cmd))

@app.command()
def reset_settings(device_address: str):
    cmd = commands.create_reset_auto_settings_command(msg_id)
    asyncio.run(_execute_command(device_address, cmd))

@app.command()
def enable_auto_mode(device_address: str):
    global msg_id
    print("Enabling auto mode")
    switch_cmd = commands.create_switch_to_auto_mode_command(msg_id)
    msg_id = commands.next_message_id(msg_id)
    time_cmd = commands.create_set_time_command(msg_id)
    asyncio.run(_execute_command(device_address, switch_cmd, time_cmd))
   
def handle_disconnect(_: BleakClient):
    print("Device was disconnected")
    # cancelling all tasks effectively ends the program
    for task in asyncio.all_tasks():
        task.cancel()

def handle_notifications(sender: BleakGATTCharacteristic, data: bytearray):
    print("Received from sender:", sender, "data:", data.hex(" "))
    if data:
        if data[0] == 91 and data[5] == 10:
            device_time = (((data[6] & 255) * 256) + (data[7] & 255)) * 60
            print("Current device time:", device_time)
        firmware_version = data[1]
        print("Current firmware version", firmware_version)

async def _execute_command(device_address: str, *commands: bytearray):
    async with BleakClient(device_address, disconnected_callback=handle_disconnect) as client:
        print("Connected to device:", device_address)

        # await client.start_notify(UART_TX_CHAR_UUID, handle_notifications)
        # print("Subscribed for notifications")
        uart_service = client.services.get_service(UART_SERVICE_UUID)
        rx_characteristic = uart_service.get_characteristic(UART_RX_CHAR_UUID)
        for command in commands:
            await client.write_gatt_char(rx_characteristic, command)
            print("Sent command", command.hex(":"))

if __name__ == "__main__":
    try:
       app()
    except asyncio.CancelledError:
        pass
