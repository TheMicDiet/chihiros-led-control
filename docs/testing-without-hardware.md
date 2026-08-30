# Testing Without Chihiros Hardware

Two layers of test tooling let you exercise the integration and the protocol
library without owning any Chihiros device.

## Home Assistant fake devices

`custom_components/chihiros/fake.py` provides in-memory devices that implement
the full `ChihirosClient` surface. They appear in the integration's device
picker when `CHIHIROS_FAKE_DEVICES=1` (the default in the Docker compose
setup; see [home-assistant-docker.md](home-assistant-docker.md)).

The roster currently covers:

- `DYDOSE` dosing pump (2/4/8 channels, manual doses, lifetime/daily counters)
- `DYVVD3` WRGB VIVID III (fan manual speed, temperature auto mode, start/stop
  temperature numbers, RPM/temperature sensors)
- `DYNW60` WRGB II, `DYWPRO60` WRGB II Pro, `DYNA2` A II
- The 2.8.59-aligned families: `DYA` A Series, `DYC` New C, `DYARGB`
  RGB+APLUS, `DYREE` RGB VIVID, `DYRGBV` RGB VIVID II, `DYSEA` SEA_LED,
  `DYONE` Commander X, `DYTWO` X300, `DYNLED` Commander 4

This is enough to test entity setup, config flow, controls, sensors, dosing,
and the fan/auto features in a live Home Assistant UI. To add another family,
extend `FAKE_DEVICES` with the model's advertised codes and channels and add a
roster assertion in `tests/test_home_assistant_unit.py`.

## Scripted BLE transport

`src/chihiros_led_control/testing.py` replaces the real Bluetooth transport
with an in-memory GATT connection, so the **real** `ChihirosDevice` client runs
its full connect flow (characteristic resolution, notification subscription,
connection prelude), command encoding, retry logic, and notification parsing
against scripted bytes.

```python
import asyncio

from chihiros_led_control.client import ChihirosDevice
from chihiros_led_control.models import WHITE_CHANNELS, DeviceModel
from chihiros_led_control.testing import ScriptedTransport


async def run() -> None:
    transport = ScriptedTransport()
    # Reply to the auth/status command with a runtime notification frame.
    transport.expect(90, 4, [1], respond=[bytes.fromhex("5b 1b 0a 00 01 0a 01 ff")])
    device = transport.make_device(DeviceModel("Test", (), WHITE_CHANNELS))
    with transport.patch_establish_connection():
        await device.query_status()
    print(device.last_runtime_notification)
    print([command.hex() for command in transport.writes])


asyncio.run(run())
```

Rules match on command id, mode byte, and an optional parameter prefix
(message id bytes and checksums are ignored). `respond` accepts frames or a
callable; `fail=True` simulates a BLE write error (and exercises the client's
retry/reconnect path). See `tests/test_scripted_transport.py` for end-to-end
examples covering query status, fan commands, dosing sequences, and retries.

The harness is excluded from the vendored HA package
(`scripts/sync_vendor.py`), so it stays a library/test-only tool.

## When you need real BLE behavior

The scripted transport does not simulate radio behavior (connection intervals,
advertisement, actual GATT round-trips). For that, run the vendor app with a
BLE sniffer to capture real frames and add them as fixtures, or drive an
ESP32 advertising the Chihiros UART service as a test peripheral.
