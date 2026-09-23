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
- Chihiros heater (`DYHET*`, `DYH1T*`) with target temperature, power, overheat-protection and calibration numbers, an auto-heating switch, a display-unit select, and current-temperature/runtime/alarm sensors
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

Dosing pumps expose one manual dose button and one dose-volume number control per
channel. The main volume sensors are `dosed today` and `total ml`, read from the
pump's status notifications. Status is requested at setup and every five minutes,
so doses made outside Home Assistant appear after the next successful readout.
Until the first readout, the sensors are unknown. `total ml` is
`total_increasing` and can be used with Home Assistant's `utility_meter`.

The locally tracked `HA manual doses today`, `HA manual dose total`, and
`HA manual dose cycles` sensors are diagnostics. They count successful manual
doses initiated by this integration and give immediate feedback, but do not
include doses made elsewhere. Existing entity IDs are retained when upgrading.
The channel's last-calibration timestamp is also a diagnostic sensor.

The first setup asks how many channels the pump has (2, 4, or 8; changeable later from the
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

### Heater (DYHET / DYH1T)

Heaters expose the controls of the vendor app as ordinary entities:

- **Temperature** (number, °C) — the manual target temperature; writing it
  switches the heater to manual mode. The device reports it back, so the number
  follows the heater whenever it is changed outside Home Assistant.
- **Power** (number, W, 10 W steps) — the manual heating power. The device never
  reports this, so the value is optimistic and restored across restarts.
- **Auto temperature** / **Auto power** (numbers, °C/W, configuration) — the
  setpoints auto mode heats towards (`initAutoDefault`). Auto mode ignores the
  manual temperature and power above, so the pair is configured and restored
  separately.
- **Mode** (select: `manual`/`auto`) — `manual` runs the manual temperature and
  power, `auto` applies the schedule stored on the device (`switchToScene`).
- **Auto heating** (switch) — arms the heating element while the heater runs in
  auto mode. This is *not* the mode switch; **Mode** is.
- **Protection temperature** (number, °C, configuration) — the overheat
  protection limit (`setHeaterProtectedTemp`).
- **Calibration temperature** (number, °C, configuration) — tell the heater
  which temperature its sensor should currently read (from a reference
  thermometer).
- **Backlight** (switch, configuration) — turns the heater's own display
  backlight on or off (the app's `deviceBacklight` toggle).
- **Temperature unit** (select) — what the heater's own display shows. Home
  Assistant always shows temperatures in the unit system configured for your
  instance, so this only affects the device itself.
- **Current temperature**, **Heating runtime** and **Alarms** (sensors) — the
  values the heater pushes. The alarm sensor's state lists the active alarms
  (`ok` when there are none) and its attributes carry the raw bitfield.
- **Reset runtime** (button) — zeroes the runtime counter after cleaning the
  heating tube (the app warns to clean past ~1944 h).

The app has two separate "auto" controls, and so does the integration. The
**Mode** select is the mode switch (`switchToScene`/`switchToManual`); the
**Auto heating** switch (`setHeaterAuto`) only decides whether the element is
allowed to heat while auto mode runs. **Mode** uses the `switchToScene` frame
the app itself sends for heaters; the app's other auto frame (`switchToAuto`) is
available as `chihirosctl heater mode <address> auto`. The device reports
neither mode nor auto-heating state, so both are optimistic and restored across
restarts.

Every manual write — the **Temperature** and **Power** numbers — sends
`switchToManual` plus the manual state frame, exactly like the vendor app, so
setting a manual value leaves auto mode and moves the **Mode** select back to
`manual`.

```yaml
# heat to 26.5 °C (each write switches the heater to manual mode)
service: number.set_value
target:
  entity_id: number.chihiros_heater_temperature
data:
  value: 26.5

# limit the heating element to 800 W
service: number.set_value
target:
  entity_id: number.chihiros_heater_power
data:
  value: 800

# run the heater on the schedule stored on the device
service: select.select_option
target:
  entity_id: select.chihiros_heater_mode
data:
  option: auto
```

The `Alarms` sensor is the automation hook for the device's fault flags — its
state is `ok` while no alarm is active, and otherwise lists the triggered
alarms (`insufficient_water`, `power_too_low`, `water_overheat`,
`needs_cleaning`, `exceeds_protection_temperature`, `heating_failure`,
`sensor_failure`).

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

# trigger a manual dose on a dosing pump: channel 1, 2.5 mL
uv run chihirosctl dosing <device-address> dose 1 2.5

# program a pump channel in one connection (active + daily volume + timer schedule)
uv run chihirosctl dosing <device-address> program 1 08:00:5.5 20:00:5.5 \
    --daily-ml 60 --weekdays monday --weekdays tuesday
uv run chihirosctl dosing <device-address> schedule 1 08:00:5.5 20:00:5.5
uv run chihirosctl dosing <device-address> active 1 --disable
uv run chihirosctl dosing <device-address> calibrate 1 --seconds 10
uv run chihirosctl dosing <device-address> reset 1

# read the pump's counters back
uv run chihirosctl dosing <device-address> totals
uv run chihirosctl dosing <device-address> today

# magnetic stirrer: start/stop, speed/pre-run, and timer schedule
uv run chihirosctl stirrer <device-address> on 1 --seconds 300
uv run chihirosctl stirrer <device-address> off 1
uv run chihirosctl stirrer <device-address> speed 1 60 --pre-seconds 30
uv run chihirosctl stirrer <device-address> schedule 1 08:00:10 20:30:5 --weekdays monday

# heater: set both manual values atomically because power cannot be read back
uv run chihirosctl heater manual-set <device-address> 26.5 800
uv run chihirosctl heater auto-defaults <device-address> 24 1000
uv run chihirosctl heater mode <device-address> manual
uv run chihirosctl heater mode <device-address> auto
uv run chihirosctl heater auto-heating <device-address> --disable
uv run chihirosctl heater backlight <device-address> --disable
uv run chihirosctl heater unit <device-address> f
uv run chihirosctl heater protector <device-address> 37
uv run chihirosctl heater calibrate <device-address> 26.0
uv run chihirosctl heater reset-work-time <device-address>

# read the heater's temperatures, runtime and alarms back
uv run chihirosctl heater status <device-address>

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
