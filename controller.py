from bleak.backends.scanner import AdvertisementData
from bleak.backends.device import BLEDevice
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak import BleakClient, BleakScanner
import sys
import datetime
import asyncio
import time


def _next_message_id(current_msg_id: tuple[bytes] = (0,0)) -> tuple[bytes]:
    msg_id_higher_byte, msg_id_lower_byte = current_msg_id;
    if msg_id_lower_byte == 255:
        if msg_id_higher_byte == 255:
            # start counting from the beginning
            return (0,1)
        if msg_id_higher_byte == 89:
            # higher byte should never be 90
            return (msg_id_higher_byte + 2, msg_id_lower_byte)
        return (msg_id_higher_byte + 1, 0)

    else:
        if msg_id_lower_byte == 89:
            # lower byte should never be 90
            return (0, msg_id_lower_byte + 2)
        return (0, msg_id_lower_byte + 1)


def _calculate_checksum(input_bytes: bytes):
    assert len(input_bytes) >= 7 # commands are always at least 7 bytes long
    checksum = input_bytes[1]
    for input_byte in input_bytes[2:]:
        checksum = checksum ^ input_byte
    return checksum
    

def _create_command_encoding(cmd_id: int, cmd_mode: int, msg_id: tuple[bytes], parameters: list[int]) -> bytearray:

    # make sure that no parameter is 90
    sanitized_params: list[int] = list(map(lambda x: x if x != 90 else 89, parameters))

    command = bytearray([cmd_id, 1, len(parameters) + 5, msg_id[0], msg_id[1], cmd_mode] + sanitized_params)

    verification_byte = _calculate_checksum(command)
    if verification_byte == 90:
        # make sure that verification byte is not 90
        new_msg_id = (msg_id[0], msg_id[1] + 1)
        return _create_command_encoding(cmd_id, cmd_mode, new_msg_id, sanitized_params)

    return command + bytes([verification_byte])
    

def _encode_timestamp(ts: datetime.datetime) -> list[int]:
    # note: day is weekday e.g. wednessday
    return [ts.year - 2000, ts.month, ts.isoweekday(), ts.hour, ts.minute, ts.second]


def create_set_time_command(msg_id: tuple[bytes]) -> bytearray:
    return _create_command_encoding(90, 9, msg_id, _encode_timestamp(datetime.datetime.now()))


def create_set_brightness_command(msg_id: tuple[bytes], brightness_level: int) -> bytearray:
    """
    set brightness
    param: brightness_level: 0 - 100
    """
    color = 0 # white
    return _create_command_encoding(90, 7, msg_id, [color, brightness_level])


def create_set_auto_command(msg_id: tuple[bytes], sunrise: datetime.time, sunset: datetime.time, brightness: int, ramp_up_minutes: int, weekdays: str) -> bytearray:
    """
    weekdays: Bit Mask (Monday Thuesday Wednessday Thursday Friday Saturday Sunday)
    """

    weekdays_int = int(weekdays, 2)
    parameters = [sunrise.hour, sunrise.minute, sunset.hour, sunset.minute, ramp_up_minutes, weekdays_int, brightness, 255, 255, 255, 255, 255, 255, 255]

    return _create_command_encoding(165, 25, msg_id, parameters)


def create_deactivate_auto_command(msg_id: tuple[bytes], sunrise: datetime.time, sunset: datetime.time, ramp_up_minutes: int, weekdays: str):
    return create_set_auto_command(msg_id, sunrise, sunset, 255, ramp_up_minutes, weekdays)


UART_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
UART_RX_CHAR_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
UART_TX_CHAR_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"
device_mac = "DC:C8:86:A8:86:86"


async def controller():

    def match_uart_service_uuid(device: BLEDevice, adv: AdvertisementData):
        # This assumes that the device includes the UART service UUID in the
        # advertising data. This test may need to be adjusted depending on the
        # actual advertising data supplied by the device.
        if UART_SERVICE_UUID.lower() in adv.service_uuids:
            return True

        return False

    device = await BleakScanner.find_device_by_filter(match_uart_service_uuid, timeout=30)

    if device is None:
        print("no matching device found, you may need to edit match_nus_uuid().")
        sys.exit(1)

    def handle_disconnect(client: BleakClient):
        print("Device was disconnected")
        # cancelling all tasks effectively ends the program
        for task in asyncio.all_tasks():
            task.cancel()

    def handle_notifications(sender: BleakGATTCharacteristic, data: bytearray):
        print("Received:", sender, data.hex(" "))
        if data:
            if data[0] == 91 and data[5] == 10:
                device_time = (((data[6] & 255) * 256) + (data[7] & 255)) * 60
                print("Current device time: ", device_time)          
            firmware_version = data[1]
            print("Current firmware version", firmware_version)
        

    async with BleakClient(device, disconnected_callback=handle_disconnect) as client:
        print("Connected")

        await client.start_notify(UART_TX_CHAR_UUID, handle_notifications)
        print("Subscribed for notifications")
    
        uart_service = client.services.get_service(UART_SERVICE_UUID)
        rx_characteristic = uart_service.get_characteristic(UART_RX_CHAR_UUID)

        msg_id = _next_message_id()
        # command = create_set_brightness_command(msg_id, 50)
        # await client.write_gatt_char(rx_characteristic, command)
        # print("Sent: set brightness ", command.hex(" "))
        # time.sleep(20)

        # msg_id = _next_message_id(msg_id)
        command = create_set_time_command(msg_id)
        await client.write_gatt_char(rx_characteristic, command)
        print("Sent set time command", command.hex(" "))

        msg_id = _next_message_id(msg_id)
        command = create_set_auto_command(
            msg_id, datetime.time(9, 0), datetime.time(18, 0), 100, 60, "0000111")
        await client.write_gatt_char(rx_characteristic, command)
        print("Sent create auto:", command.hex(" "))

        time.sleep(60)

        msg_id = _next_message_id(msg_id)
        command = create_deactivate_auto_command(
            msg_id, datetime.time(9, 0), datetime.time(18, 0), 60, "0000111")
        await client.write_gatt_char(rx_characteristic, command)
        print("Sent delete auto:", command.hex(" "))


if __name__ == "__main__":
    try:
        asyncio.run(controller())
    except asyncio.CancelledError:
        print("Canceled")


