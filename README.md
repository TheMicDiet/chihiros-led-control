# Chihiros LED Control

This repository contains a python **CLI** script as well as a **Home Assistant integration** that can be used to control Chihiros LEDs for aquariums via bluetooth without the vendor app. For this purpose, the protocol to control the LED has been reversed engineered with the help of decompiling the old *Magic App* as well as sniffing and analyzing of bluetooth packages that are sent by the new *My Chihiros App*. The new app is based on flutter and only contains a binary that can not easily be analyzed.

## Supported Devices
- [Chihiros LED A2](https://www.chihirosaquaticstudio.com/products/chihiros-a-ii-built-in-bluetooth)
- [Chihiros WRGB II](https://www.chihirosaquaticstudio.com/products/chihiros-wrgb-ii-led-built-in-bluetooth) (Regular, Pro, Slim; Pro is true WRGB)
- Chihiros Tiny Terrarium Egg
- Chihiros C II (RGB, White)
- Chihiros Universal WRGB
- Chihiros Z Light TINY
- Chihiros Commander 1
- Chihiros Commander 4
- other LED models might work as well but are not tested


## Using the Home Assistant integration
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=themicdiet&repository=chihiros-led-control&category=Integration)
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

### Home Assistant services

The integration provides services for changing the auto mode schedule from
**Developer Tools -> Actions** or from automations:

- `chihiros.add_schedule`: add one schedule period.
- `chihiros.remove_schedule`: remove one schedule period.
- `chihiros.reset_schedule`: remove all schedule periods.
- `chihiros.set_schedule`: replace the complete schedule.

If only one Chihiros device is configured, `entry_id` and `address` can be
omitted. If multiple devices are configured, include either the config entry ID
or Bluetooth address.

Replace the complete schedule:

```yaml
service: chihiros.set_schedule
data:
  address: "AA:BB:CC:DD:EE:FF"
  periods:
    - start: "08:00"
      end: "12:00"
      brightness: 40
      ramp_up_minutes: 30
      weekdays:
        - monday
        - tuesday
    - start: "09:00"
      end: "17:00"
      brightness: 55
      weekdays:
        - wednesday
        - thursday
```

Add one white or shared-brightness period:

```yaml
service: chihiros.add_schedule
data:
  start: "08:00"
  end: "18:30"
  brightness: 70
  ramp_up_minutes: 30
  weekdays:
    - monday
    - tuesday
```

Remove a matching period:

```yaml
service: chihiros.remove_schedule
data:
  start: "08:00"
  end: "18:30"
  ramp_up_minutes: 30
  weekdays:
    - monday
    - tuesday
```

Reset all schedule periods:

```yaml
service: chihiros.reset_schedule
data:
  address: "AA:BB:CC:DD:EE:FF"
```

Schedule writes are validated before sending commands to the device. Unsupported
channels, invalid brightness values, invalid weekdays, empty replacement
schedules, and multiple replacement periods for the same weekday are rejected.
Known devices replace the previous period for a weekday when another one is
written, so `set_schedule` accepts at most one period per weekday. After writing
a schedule, enable the `Auto Mode` switch to run it.

## Requirements
- a device with bluetooth LE support for sending the commands to the LED
- [uv](https://docs.astral.sh/uv/) for Python environment and dependency management

## Using the CLI
```bash
# setup the environment
uv sync --extra cli

# show help
uv run chihirosctl --help

# discover devices and their address
uv run chihirosctl list-devices

# turn on the device
uv run chihirosctl turn-on <device-address>

# turn off the device
uv run chihirosctl turn-off <device-address>

# manually set the brightness to 100
uv run chihirosctl set-brightness <device-address> 100

# create an automatic timed setting that turns on the light from 8:00 to 18:00 at brightness 100
uv run chihirosctl add-setting <device-address> 8:00 18:00 100

# create a setting for specific weekdays with maximum brightness of 75 and ramp up time of 30 minutes
uv run chihirosctl add-setting <device-address> 9:00 18:00 75 --weekdays monday --weekdays tuesday --ramp-up-in-minutes 30

# manually set the brightness to 60 red, 80 green, 100 blue on RGB models
uv run chihirosctl set-brightness <device-address> 60 80 100

# create an automatic timed setting that turns on the light from 8:00 to 18:00
uv run chihirosctl add-setting <device-address> 8:00 18:00 100 100 100

# create a setting for specific weekdays with maximum brightness of 35, 55, 75 and ramp up time of 30 minutes
uv run chihirosctl add-setting <device-address> 9:00 18:00 35 55 75 --weekdays monday --weekdays tuesday --ramp-up-in-minutes 30

# on true WRGB models, set red, green, blue, and white levels
uv run chihirosctl add-setting <device-address> 9:00 18:00 35 55 75 40

# enable auto mode to activate the created timed settings
uv run chihirosctl enable-auto-mode <device-address>

# delete a created setting
uv run chihirosctl remove-setting <device-address> 8:00 18:00

# reset all created settings
uv run chihirosctl reset-settings <device-address>

```

## Protocol

The Bluetooth command format and known modes are documented in
[docs/protocol.md](docs/protocol.md).

## Contributing
Reusable library and CLI code lives in `src/chihiros_led_control/`. The Home
Assistant integration lives in `custom_components/chihiros/` and imports the
vendored runtime copy from `custom_components/chihiros/vendor/` so HACS installs
do not require the top-level package.

Set up the development environment with uv:

```bash
uv sync --group dev
uv run --group dev pytest
uv run --group dev pre-commit run --all-files
```

After changing library code, refresh the vendored copy:

```bash
uv run python scripts/sync_vendor.py
uv run python scripts/sync_vendor.py --check
```

For local Home Assistant testing with Docker Compose, see [docs/home-assistant-docker.md](docs/home-assistant-docker.md).

Successful pushes to `main` create an automatic GitHub release after the `HA Validation` workflow passes. The release workflow reads `custom_components/chihiros/manifest.json`, creates a tag named `v<version>`, and uses GitHub generated release notes. If that tag already exists, the release is skipped.

See [docs/architecture.md](docs/architecture.md) for the package layout.
