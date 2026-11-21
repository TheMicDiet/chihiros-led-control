# Testing Without Chihiros Hardware

Test the real code at its public boundaries. Simulate the external BLE
peripheral in memory; do not mock or monkeypatch drivers, services, entities,
clocks, or storage. Home Assistant runs with real config entries and disk
storage in an isolated directory.

## Test boundaries

Each test must have exactly one layer marker. Mark Home Assistant tests with
`homeassistant` as well to select the right dependency group.

| Marker | Boundary | Expect |
| --- | --- | --- |
| `unit` | BLE codecs, advertised model detection, and public validation | Wire bytes, decoded fields, or a specific validation error |
| `component` | Driver and transport, CLI, or maintenance command | Ordered GATT transcript, notifications, command output, or files |
| `integration` | Real HA config flows, services, and lifecycle | Published state, reload behavior, service errors, or GATT traffic |

HA-dependent modules must call `pytest.importorskip("homeassistant")` before
importing HA-only dependencies, including `voluptuous`. Pytest imports modules
before applying `-m`, so markers alone cannot protect the library-only job.
Verify dependency isolation in a fresh environment containing only the `test`
group; an existing `dev` environment can mask missing-dependency failures.

Put the expected output next to the action. Use literal bytes from the protocol
or a capture, small ordered transcripts, and specific exceptions. Leave out
unrelated or variable fields (such as per-session message IDs) unless they are
part of the behavior under test. Don't generate expected wire bytes with the
encoder under test.

Keep one scenario at the lowest public boundary that can reproduce a failure.
Do not repeat codec matrices through the CLI or HA. Keep separate scenarios for
retries, cancellation, and writes that may already have reached a device.

```bash
uv --cache-dir .uv-cache run --group test pytest -m 'not homeassistant'
uv --cache-dir .uv-cache run --group ha-test pytest -m homeassistant
uv --cache-dir .uv-cache run --group dev pytest
```

CI measures branch coverage against production code, not the vendor copy or
hardware simulators. Physical BLE/app verification remains manual; the automated
suite cannot prove radio or firmware behavior.

## Home Assistant Bluetooth proxy tests

`tests/ha_peripheral.py` registers an in-memory Bluetooth proxy through the
normal HA Bluetooth API. It advertises devices and supplies a Bleak backend:
config flows, services, registries, the vendored driver, and the BLE transport
all run unchanged. `tests/test_ha_boundary.py` checks public HA state and writes
observed at the peripheral. Use this path for regressions that need HA.

## Home Assistant demo devices

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

These devices are for manual UI exploration in the Docker demo. To add another
family, extend `FAKE_DEVICES` with its advertised codes and channels.

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
retry/reconnect path). Use `realtime=False` to skip hardware-only waits and
`pause(...)` to hold a specific pending GATT operation for a concurrency test.
See `tests/test_scripted_transport.py` for examples.

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

The boundary regression tests also exercise cancellation cleanup, concurrent
schedule edits, paired settings, and restoration across HA reloads.

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

`test_led_levels.py`, `test_ha_boundary.py`, and
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
