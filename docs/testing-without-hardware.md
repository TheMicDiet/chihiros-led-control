# Testing Without Chihiros Hardware

The automated suite follows a test pyramid: broad isolated unit coverage, fewer
component scenarios, and focused Home Assistant integration journeys. Fake
devices and scripted BLE make every automated layer hardware-independent.

## Test pyramid

Each test must have exactly one layer marker. Collection fails for missing or
conflicting layers, including tests deselected with `-m`; unknown markers also
fail collection.

| Marker | Boundary | Coverage belongs here |
| --- | --- | --- |
| `unit` | One behavior; external collaborators replaced | Codec bytes, parsing, validation, model selection, RGB/RGBW arithmetic, isolated entity state and errors |
| `component` | Several real local components; hardware/network replaced | Driver plus production scripted transport, CLI adapter, HA storage/dispatcher, service helper through vendored driver |
| `integration` | Real HA config-entry/service/entity lifecycle | Service routing, entity registration, restoration/reload, setup/unload cleanup and cross-entity behavior |

`homeassistant` is an independent dependency marker, not a test level. An
isolated entity test can be both `unit` and `homeassistant`; a scripted
transport test is `component`, not an end-to-end hardware test.

Place each regression at the lowest boundary that can reproduce the observable
failure. Keep validation and arithmetic matrices in unit tests. Use component
tests for transport ordering, retries, cancellation and notification delivery.
Keep integration cases when HA service dispatch, lifecycle or interactions
between entities are essential to the failure. Do not duplicate a full
lower-level matrix through config-entry setup, remove meaningful scenarios to
meet a ratio, or add trivial tests to inflate the base.

Before adding a scenario, compare its preconditions, action, observable result
and failure mode with existing cases. Similar inputs or shared setup alone do
not make two tests duplicates. When a stronger scenario subsumes another, keep
the stronger one and transfer any unique assertions before removing the subset.
Keep separate adapter/lifecycle tests only when they protect a distinct routing,
state, persistence or cleanup contract.

Prefer assertions about emitted bytes, applied state, persistence and
user-visible errors over exact source/configuration snapshots. Validation tests
should name the expected exception type; unrelated failures must not satisfy
them. Preserve boundary values and race/cancellation orderings instead of
repeating ordinary values from the same equivalence class.

Run the fast library base without installing Home Assistant:

```bash
uv --cache-dir .uv-cache run --group test pytest -m "unit and not homeassistant"
uv --cache-dir .uv-cache run --group test pytest -m "component and not homeassistant"
```

Run Home Assistant layers in increasing scope:

```bash
uv --cache-dir .uv-cache run --group ha-test pytest -m "unit and homeassistant"
uv --cache-dir .uv-cache run --group ha-test pytest -m "component and homeassistant"
uv --cache-dir .uv-cache run --group ha-test pytest -m "integration and homeassistant"
```

`uv --cache-dir .uv-cache run --group dev pytest` runs all automated layers.
CI runs each layer explicitly, units before components before integration, and
combines coverage within each dependency group without lowering its threshold.
Physical BLE/app verification remains a small manual top layer; the automated
suite cannot prove radio or firmware behavior.

## Home Assistant fake devices

`custom_components/chihiros/fake/` provides in-memory devices that implement
family-specific fake device surfaces. They appear in the integration's device
picker when `CHIHIROS_FAKE_DEVICES=1` (the default in the Docker compose
setup; see [home-assistant-docker.md](home-assistant-docker.md)).

The roster currently covers:

- `DYDOSE` dosing pump (2/4/8 channels, manual doses, lifetime/daily counters)
- `DYVVD3` WRGB VIVID III (fan manual speed, temperature auto mode, start/stop
  temperature numbers, RPM/temperature sensors)
- `DYNW60` WRGB II, `DYWPRO60` WRGB II Pro, `DYNA2` A II
- `DYMIXR` magnetic stirrer (per-channel stir switches, speed/pre-run numbers,
  timer schedules)
- `DYHET` heater (shared target/configured-power numbers, mode and display-unit
  selects, automatic power increase, measured-power/temperature/runtime/alarm
  sensors, and the reset-runtime button)
- The 2.8.59-aligned families: `DYA` A Series, `DYC` New C, `DYARGB`
  RGB+APLUS, `DYREE` RGB VIVID, `DYRGBV` RGB VIVID II, `DYSEA` SEA_LED,
  `DYONE` Commander X, `DYTWO` X300, `DYNLED` Commander 4

This is enough to test entity setup, config flow, controls, sensors, dosing,
and the fan/auto features in a live Home Assistant UI. To add another family,
extend `FAKE_DEVICES` with the model's advertised codes and channels and add a
roster assertion in `tests/test_home_assistant_unit.py`.

## Scripted BLE transport

`src/chihiros_led_control/testing.py` provides `ScriptedTransport`, which
supplies a scripted BLE client to the production `BleTransport`. Real family
drivers and the production transport run connection setup, retries, locking,
idle disconnect, command pacing, and notification parsing against scripted
bytes.

```python
import asyncio

from chihiros_led_control import ChihirosDevice
from chihiros_led_control.models import DeviceModel, LedSpec, WHITE_CHANNELS
from chihiros_led_control.testing import ScriptedBLEDevice, ScriptedTransport

async def run() -> None:
    transport = ScriptedTransport()
    # Reply to the auth/status command with a runtime notification frame.
    transport.expect(90, 4, [1], respond=[bytes.fromhex("5b 1b 0a 00 01 0a 01 ff")])
    device = ChihirosDevice(
        ScriptedBLEDevice(transport.name, transport.address),
        DeviceModel("Test", (), LedSpec(WHITE_CHANNELS)),
        transport=transport,
    )
    await device.query_status()
    # Successful commands reuse the connection until it goes idle.
    await device.disconnect()
    print(device.last_runtime_notification)
    print([command.hex() for command in transport.writes])


asyncio.run(run())
```

Rules match on command id, mode byte, and an optional parameter prefix
(message id bytes and checksums are ignored). `respond` accepts frames or a
callable; `fail=True` simulates a BLE write error (and exercises the client's
retry/reconnect path). See `tests/test_scripted_transport.py` for component
examples covering query status, fan commands, dosing sequences, and retries.

Successful transactions reuse the configured connection. Call
`await device.disconnect()` in a test when the lifecycle boundary itself is
under test; retries and failed setup paths disconnect before reconnecting.

The harness is excluded from the vendored HA package
(`scripts/sync_vendor.py`), so it stays a library/test-only tool.

## Pump replay and partial-write checks

Run `uv --cache-dir .uv-cache run --no-sync python scripts/verify_manual_dose.py`
after syncing dependencies. It explores ambiguous BLE writes for manual doses,
timed calibration, and both run frames sent through the raw slave broadcast
path. The bounds cover 729 manual-dose sequences and nine sequences for each
single-frame operation, totaling 756 scenarios. It checks one channel and one
value per operation, not physical pump behavior or slave firmware deduplication.

The state-machine regression tests additionally exercise cancellation cleanup,
concurrent schedule edits and paired settings, delayed midnight callbacks,
storage failures after hardware success, and deferred startup restoration.

`program_channel` is a separate, non-atomic batch. In a scripted run with an
active setting, daily volume, and one schedule point, failures before every
daily-volume write accepted only the active-setting frames. Failures after
every schedule write accepted all three frames three times before reporting an
error. A retry after an initial daily-volume failure sent the complete batch.
These traces establish what the driver writes, not how firmware applies
repeated schedule frames or whether a successful retry converges device state.
Do not assume a failed programming call left the previous schedule intact.

## When you need real BLE behavior

The scripted transport does not simulate radio behavior (connection intervals,
advertisement, actual GATT round-trips). For that, run the vendor app with a
BLE sniffer to capture real frames and add them as fixtures, or drive an
ESP32 advertising the Chihiros UART service as a test peripheral.

## Issue #133 regression scenarios

`test_led_levels.py`, `test_led_levels_ha.py`, and
`test_schedule_lifecycle.py` exercise model-unit conversion, scene/custom-curve
selection, safe validation failures, and authoritative Lite reconnect replay.
The heater and scripted-transport suites cover backlight scheduling, VIVID
switch polarities, FFE1 subscription, and unescaped framing.

A smoke session through the real Home Assistant service registry, coordinator,
vendored LED driver, and production transport with scripted BLE emitted:

```text
a5 01 13 00 02 19 08 00 17 3b 00 7f 73 41 00 ff ff ff ff ff 9f
5a 01 08 00 0a 06 02 11 c8 de
5a 01 08 00 0b 06 02 14 00 12
```

The scene represents 08:00–midnight (encoded 23:59), RGB service percentages
100/50/0 converted to raw 115/65/0. The custom points set blue at 08:30 to
raw 200 (100%) and at 10:00 to zero. Each replacement selected its respective
automatic controller and left the coordinator's `auto_mode` true.

These are scripted output frames, not official-app HCI captures. No physical
Bluetooth adapter or representative Chihiros hardware was available for this
verification. Radio behavior, protection behavior, and legacy-hardware framing
still require the comparison described in
[capturing-ble-traffic.md](capturing-ble-traffic.md).
