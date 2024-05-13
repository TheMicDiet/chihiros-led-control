# Chihiros LED Control

This repository contains a python **CLI** script as well as a **Home Assistant integration** that can be used to control Chihiros LEDs for aquariums via bluetooth without the vendor app. For this purpose, the protocol to control the LED has been reversed engineered with the help of decompiling the old *Magic App* as well as sniffing and analyzing of bluetooth packages that are sent by the new *My Chihiros App*. The new app is based on flutter and only contains a binary that can not easily be analyzed.


## Supported Devices
- [Chihiros LED A2](https://www.chihirosaquaticstudio.com/products/chihiros-a-ii-built-in-bluetooth)
- [Chihiros WRGB II](https://www.chihirosaquaticstudio.com/products/chihiros-wrgb-ii-led-built-in-bluetooth)
- Chihiros Tiny Terrarium Egg
- other LED models might work as well but are not tested

## Requirements
- a device with bluetooth LE support for sending the commands to the LED
- [Python 3](https://www.python.org/downloads/) with pip

## Using the Home Assistant integration
### Setup with HACS
- Inside HACS add this repository as a custom repository: ```HACS -> Integrations -> 3 dots on the top right-> Custom repositories```
- Search for ```Chihiros``` in the repositories and download it
- Restart Home Assistant
- Go to the integrations user interface and add the Chihiros integration
- Supported devices should be discovered at this point

### Manual Setup
- Copy the directory ```custom_components/chihiros``` to your ```<config dir>/custom_components``` directory
- Restart Home-Assistant
- Add the Chihiros integration to your Home Assistant instance via the integrations user interface

## Using the CLI
```bash
# setup the environment
python -m venv venv
source venv/bin/activate
pip install -e .

# show help
chihirosctl --help

# discover devices and their address
chihirosctl list-devices

# turn on the device
chihirosctl turn-on <device-address>

# turn off the device
chihirosctl turn-off <device-address>

# manually set the brightness to 100
chihirosctl set-brightness <device-address> 100

# create an automatic timed setting that turns on the light from 8:00 to 18:00
chihirosctl add-setting <device-address> 8:00 18:00

# create a setting for specific weekdays with maximum brightness of 75 and ramp up time of 30 minutes
chihirosctl add-setting <device-address> 9:00 18:00 --weekdays monday --weekdays tuesday --ramp-up-in-minutes 30 --max-brightness 75

# on RGB models, use the RGB versions of the above commands

# manually set the brightness to 60 red, 80 green, 100 blue on RGB models
chihirosctl set-rgb-brightness <device-address> 60 80 100

# create an automatic timed setting that turns on the light from 8:00 to 18:00
chihirosctl add-rgb-setting <device-address> 8:00 18:00

# create a setting for specific weekdays with maximum brightness of 35, 55, 75 and ramp up time of 30 minutes
chihirosctl add-rgb-setting <device-address> 9:00 18:00 --weekdays monday --weekdays tuesday --ramp-up-in-minutes 30 --max-brightness 35 55 75

# enable auto mode to activate the created timed settings
chihirosctl enable-auto-mode <device-address>

# delete a created setting
chihirosctl delete-setting <device-address> 8:00 18:00

# reset all created settings
chihirosctl reset-settings <device-address>

```

## Protocol
The vendor app uses Bluetooth LE to communicate with the LED. The LED advertises a UART service with the UUID `6E400001-B5A3-F393-E0A9-E50E24DCCA9E`. This service contains a RX characteristic with the UUID `6E400002-B5A3-F393-E0A9-E50E24DCCA9E`. This characteristic can be used to send commands to the LED. The LED will respond to commands by sending a notification to the corresponding TX service with the UUID `6E400003-B5A3-F393-E0A9-E50E24DCCA9E`.


The commands are sent as a byte array with the following structure:


| Command ID | 1 | Command Length | Message ID High | Message ID Low | Mode | Parameters | Checksum |
| --- | --- | --- | --- | --- | --- | --- | --- |


The checksum is calculated by XORing all bytes of the command together. The checksum is then added to the command as the last byte.

The message id is a 16 bit number that is incremented with each command. It is split into two bytes. The first byte is the high byte and the second byte is the low byte.

The command length is the number of parameters + 5.

### Manual Mode
The LED can be set to a specific brightness by sending the following command with the following options:
- Command ID: **90**
- Mode: **7**
- Parameters: [ **Color** (0-2), **Brightness** (0 - 100)]

On non-RGB models, the color parameter should be set to 0 to indicate white. On RGB models, each color's brightness is sent as a separate command. Red is 0, green is 1, blue is 2.

### Auto Mode
To switch to auto mode, the following command can be used:
- Command ID: **90**
- Mode: **5**
- Parameters: [ **18**, **255**, **255** ]

With auto mode enabled, the LED can be set to automatically turn on and off at a specific time. The following command can be used to create a new setting:

- Command ID: **165**
- Mode: **25**
- Parameters: [ **sunrise hour**, **sunrise minutes**, **sunset hour**, **sunset minutes**, **ramp up minutes**, **weekdays**, **red brightness**, **green brightness**, **blue brightness**, 5x **255** ]

The weekdays are encoded as a sequence of 7 bits with the following structure: `Monday Thuesday Wednesday Thursday Friday Saturday Sunday`. A bit is set to 1 if the LED should be on on that day. It is only possible to set one setting per day i.e. no conflicting settings. There is also a maximum of 7 settings.

On non-RGB models, the desired brightness should be set as the red brightness while the other two colors should be set to **255**.

To deactivate a setting, the same command can be used but the brightness has to be set to **255**.

#### Set Time
The current time is required for auto mode and can be set by sending the following command:

- Command ID: **90**
- Mode: **9**
- Parameters: [ **year - 2000**, **month**, **weekday**, **hour**, **minute**, **second** ]

- Weekday is 1 - 7 for Monday - Sunday

#### Reset Auto Mode Settings
The auto mode and its settings can be reset by sending the following command:
- Command ID: **90**
- Mode: **5**
- Parameters: [ **5**, **255**, **255** ]
