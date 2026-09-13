# Chihiros LED Control

This repository contains a python **CLI** script as well as a **Home Assistant integration** that can be used to control Chihiros LEDs for aquariums via bluetooth without the vendor app. It also includes first Home Assistant support for Chihiros dosing pumps. For this purpose, the protocol to control the devices has been reversed engineered and is based on both the old *Magic App* and the new *My Chihiros App*: the old app was decompiled from its Android sources, the new app's Flutter binary was unpacked to extract and analyze its Dart snapshot, and the resulting frame encoding was verified byte-for-byte against the app's `dataMaker.dart` logic as well as sniffed Bluetooth traffic.

> [!WARNING]
> This is an independent, unofficial, community-developed project. It is not
> affiliated with, endorsed by, sponsored by, or otherwise associated with
> Chihiros Aquatic Studio.
>
> “Chihiros” and any other product names or trademarks referenced by this
> project belong to their respective owners.
>
> This software is provided “as is” and without warranties of any kind, whether
> express or implied. You use it entirely at your own risk. The authors and
> contributors are not responsible for any damage, loss, injury, equipment
> malfunction, data loss, or other consequences resulting from the use of this
> project, including damage to aquarium equipment, livestock, or other property.

## Supported Devices
- Chihiros A Series
- Chihiros C II (RGB, White)
- Chihiros Commander 1
- Chihiros Commander 4
- Chihiros Commander X
- Chihiros dosing pump (`DYDOSE*`, `DYNDOS`) with first Home Assistant support for manual dosing, per-channel calibration, daily dose totals, and lifetime pump cycle/ml counters
- Chihiros magnetic stirrer (`DYMIXR*`) with per-channel stir switches, speed and pre-run numbers, timer schedule programming, and master/slave mirroring of a linked dosing pump
- [Chihiros LED A2](https://www.chihirosaquaticstudio.com/products/chihiros-a-ii-built-in-bluetooth)
- Chihiros New C
- Chihiros RGB+APLUS
- Chihiros RGB VIVID
- Chihiros RGB VIVID II
- Chihiros SEA_LED (WRGB)
- Chihiros Tiny Terrarium Egg
- Chihiros Universal WRGB
- [Chihiros WRGB II](https://www.chihirosaquaticstudio.com/products/chihiros-wrgb-ii-led-built-in-bluetooth) (Regular, Pro, Slim; Pro is true WRGB)
- Chihiros WRGB VIVID III (true WRGB, including fan speed control and fan RPM/temperature sensors)
- Chihiros X300 (white/warm)
- Chihiros Z Light TINY
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
- `chihiros.set_auto_curve`: replace the device's stored auto curve with the
  vendor app's per-point `0x5A/0x06` format (channel id → list of
  `[minutes, level]` pairs). Clears the stored curve first. The curve is
  stored but only active while the device is in auto mode (toggle the
  Auto Mode switch).

> **Upgrade note:** Commander 4 (`DYLED`/`DYNLED`) now auto-detects its
> verified 4-channel layout instead of asking for a generic device type.
> Existing entries that had picked "white" or "rgb" get a single RGBW light
> entity (unique id `-rgbw`) replacing the old per-channel entity; entries
> that picked "wrgb" are unaffected.

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

Dosing pumps expose one manual dose button, one dose-volume number control, and
the following locally tracked sensors per pump channel:

- `dosed today` (volume in mL, reset at local midnight)
- `total ml` (cumulative lifetime volume, `total_increasing`)
- `total cycles` (cumulative lifetime dose count, `total_increasing`)

The lifetime `total ml` and `total cycles` sensors are `total_increasing`, so they
can be fed directly into the Home Assistant `utility_meter` to derive daily,
weekly, monthly, or yearly consumption sensors. The first setup asks
how many channels the pump has (2, 4, or 8; changeable later from the
integration's Configure dialog). Manual doses can also be triggered
from automations with `chihiros.dose_ml`:

```yaml
service: chihiros.dose_ml
data:
  address: "AA:BB:CC:DD:EE:FF"
  pump: 1
  ml: 2.5
```

#### Calibrating a dosing pump

Each dosing pump exposes a **Calibrate pump** button. Pressing it raises a
Home Assistant repair notification; opening it (from the bell, or
**Settings → System → Repairs**) runs the wizard in place. The wizard replays
the vendor app's calibration:

1. **Calibration dose** — a fixed 5-second timed run.
2. **Enter the measured volume** — what the pump dispensed, to the nearest
   0.05 ml.
3. **Dose 4ml test** — a fixed 4 ml manual dose (counted in the daily totals).
4. **"Was this accurate?"** — *Yes* finishes the wizard, *No* restarts it
   from step 1.

While a stirrer slave is linked, every calibration frame is broadcast to the
stirrer verbatim, like the vendor app. A per-channel `last calibration`
sensor shows when each channel was last calibrated. Magnetic stirrers are not
calibratable (matching the app).

Magnetic stirrers (`DYMIXR`) expose one stir switch and a speed number
(0-100 %, device default 40) per channel; the pre-run number (0-999 s) is
created **disabled by default** because it only matters while the stirrer
runs as a slave of a linked dosing pump. Setup asks how many stir channels to
expose (2, 4, or all 8; changeable later from the integration's Configure
dialog). The stirrer sends no status notifications, so stirrer states are
optimistic and restored across Home Assistant restarts. Do not build
automations on the switch *state* — use it (or `chihiros.stir_for`) to
*drive* the channel.

```yaml
service: chihiros.stir_for
data:
  device_id: <device id>
  channel: 1
  duration: "00:05:00"   # or seconds: 300
```

Replace a channel's timer schedule with `chihiros.set_stir_schedule`; run
times are given in minutes and must be at least 2 minutes apart (the
repetition is picked with a `weekdays` selector):

```yaml
service: chihiros.set_stir_schedule
data:
  device_id: <device id>
  channel: 1
  weekdays:
    - everyday
  points:
    - start: "08:00"
      minutes: 30
    - start: "20:00"
      minutes: 15
```

All services accept a Home Assistant `device` target (`device_id`) in
addition to the raw `entry_id`/`address` fields, so they can be picked from
the UI device selector. The "first setting of the day" flag is derived from
the integration's programming record and no longer needs to be passed.

### Master/slave mirroring (pump → stirrer)

The vendor app mirrors a linked stirrer by broadcasting the pump's programming
frames. Home Assistant cannot broadcast, so mirroring is explicit:

- `chihiros.set_dosing_schedule` and `chihiros.set_channel_active` program a
  pump channel and replay the write to linked stirrers automatically.
- `chihiros.set_stirrer_master` links a stirrer to a pump and replays the
  pump's recorded programming on linking; omit the master fields to unlink.
- `chihiros.mirror_stirrer` replays the pump's full recorded programming on
  demand (including the dose-delay frame).
- `chihiros.dose_ml` and `chihiros.reset_dosing_channel` broadcast their
  frames verbatim to linked stirrers; the stirrer treats a broadcast manual
  dose as an immediate stir.

#### Stir-before-dose timing

There are no coordination frames between a linked pump and stirrer — both run
the same mirrored schedule times on their own clocks, and ordering comes from
programmed offsets:

- the stirrer can start `pre_second` seconds **before** each schedule point
  (the per-channel `Stir channel N pre-run` number; default 0);
- the pump can **wait** before dosing each supplement when its dose-delay flag
  is set (`chihiros.set_dose_delay`, mirrored to the stirrer on linking).

So stirring precedes dosing only if the pre-run is set and/or the dose delay
is enabled — at the defaults, both devices act at the same instant.

```yaml
# Link the stirrer to the pump and mirror the pump's current programming:
service: chihiros.set_stirrer_master
data:
  address: "AA:BB:CC:DD:EE:FF"   # stirrer
  master_address: "11:22:33:44:55:66"

# Program the master — the stirrer follows automatically:
service: chihiros.set_dosing_schedule
data:
  address: "11:22:33:44:55:66"   # pump
  channel: 1
  mode: timer
  points:
    - start: "08:00"
      ml: 2.5
    - start: "20:00"
      ml: 1.0
```

The link is persisted bookkeeping only (no BLE pairing frame exists); the
vendor app and Home Assistant links are independent.
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

# trigger a manual dose on a dosing pump: pump 1, 2.5 mL
uv run chihirosctl dose-ml <device-address> 1 2.5

# program a dosing pump channel (timer mode: HH:MM:ML points) and its daily volume
uv run chihirosctl doser-schedule <device-address> 1 08:00:5.5 20:00:5.5
uv run chihirosctl doser-daily-dose <device-address> 1 60
uv run chihirosctl doser-active <device-address> 1 --disable
uv run chihirosctl doser-calibrate <device-address> 1 --seconds 10
uv run chihirosctl doser-reset-channel <device-address> 1

# read the pump's counters back
uv run chihirosctl doser-totals <device-address>
uv run chihirosctl doser-today <device-address>

# magnetic stirrer: start/stop, speed/pre-run, and timer schedule
uv run chihirosctl stir-on <device-address> 1 --seconds 300
uv run chihirosctl stir-off <device-address> 1
uv run chihirosctl stir-speed <device-address> 1 60 --pre-seconds 30
uv run chihirosctl stir-schedule <device-address> 1 08:00:10 20:30:5

```

## Protocol

The Bluetooth command format and known modes are documented in
[docs/protocol.md](docs/protocol.md). Command encodings were verified against
the 2.8.59 build of the official My Chihiros app (see
[docs/protocol.md](docs/protocol.md) for the app-verified details).

## Contributing
Reusable library and CLI code lives in `src/chihiros_led_control/`. The Home
Assistant integration lives in `custom_components/chihiros/` and imports the
vendored runtime copy from `custom_components/chihiros/vendor/` so HACS installs
do not require the top-level package.

Want to help add support for a device that is not listed yet? See
[docs/capturing-ble-traffic.md](docs/capturing-ble-traffic.md) for
step-by-step instructions on recording the Bluetooth traffic between the
official My Chihiros app and the device. Captures like these are how new
device protocols get reverse-engineered.

Set up the development environment with uv:

```bash
uv --cache-dir .uv-cache sync --group dev
uv --cache-dir .uv-cache run --group dev pytest
uv --cache-dir .uv-cache run --group dev pre-commit run --all-files
```

Home Assistant integration tests use the separate `ha-test` dependency group
because they install Home Assistant and its test-time runtime dependencies. Run
them explicitly when changing files under `custom_components/chihiros/`:

```bash
uv --cache-dir .uv-cache run --group ha-test pytest tests/test_home_assistant_integration.py tests/test_manifest_requirements.py
```

The integration test creates a temporary Home Assistant config directory,
symlinks this repository's `custom_components/` directory into it, and patches
storage writes so the test does not need a running Home Assistant instance or
real Bluetooth hardware. The manifest requirements test keeps the integration's
runtime requirement pins aligned with `pyproject.toml`.

After changing library code, refresh the vendored copy:

```bash
uv --cache-dir .uv-cache run python scripts/sync_vendor.py
uv --cache-dir .uv-cache run python scripts/sync_vendor.py --check
```

For local Home Assistant testing with Docker Compose, see [docs/home-assistant-docker.md](docs/home-assistant-docker.md).
For testing without any Chihiros hardware (fake devices and a scripted BLE transport), see [docs/testing-without-hardware.md](docs/testing-without-hardware.md).

Successful pushes to `main` create an automatic GitHub release after the `HA Validation` workflow passes. The release workflow reads `custom_components/chihiros/manifest.json`, creates a tag named `v<version>`, and uses GitHub generated release notes. If that tag already exists, the release is skipped.

See [docs/architecture.md](docs/architecture.md) for the package layout.
