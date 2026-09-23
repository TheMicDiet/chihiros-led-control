# Device Architecture Implementation Plan

## Goal

Refactor toward a metadata-driven, family-oriented architecture without changing:

- BLE command bytes or sequencing;
- retry and non-idempotent command behavior;
- connection prelude order;
- notification semantics;
- Home Assistant entity IDs, services, storage keys, or UI behavior;
- top-level public exports such as `ChihirosDevice`, `ChihirosDosingPump`, `DeviceModel`, `create_device()`, and `detect_model()`.

Target dependency direction:

```text
models/profile types
        ↓
device registry
        ↓
factory
        ↓
family drivers ─────→ family codecs ─────→ frame encoder
        │
        └────────────→ transport
```

No dependency in the opposite direction: transport and codecs must not import concrete device drivers or Home Assistant code.

## Architectural decisions

### Preserve `ChihirosDevice` as the LED driver

To minimize public API breakage:

```text
BaseChihirosDevice        # internal, common runtime behavior only
├── ChihirosDevice        # existing public LED client
├── ChihirosDosingPump
├── ChihirosMagStirrer
└── ChihirosHeater
```

`ChihirosDevice` remains the exported LED client. Pump, stirrer, and heater stop inheriting LED methods.

Direct imports from package-level or family-driver imports will be migrated to package-level imports or the new family modules. Do not retain a compatibility module; document the module-path break if external users rely on it.

### Share protocol code, not domain inheritance

`ChihirosMagStirrer` and `ChihirosDosingPump` become siblings. Both use dosing protocol builders where their wire format overlaps.

### Keep model identity separate from protocol behavior

A product such as WRGB II is not itself a protocol type. Its profile records:

- advertised prefixes;
- product name;
- device kind;
- LED protocol variant;
- channel layout;
- optional features.

### Defer unrelated behavior changes

Not part of this refactor:

- adding CO₂, chiller, rain, gateway, or AWS support;
- AIX transport implementation;
- changing NewBleLed level normalization;
- changing schedule semantics;
- adding or removing Home Assistant entities;
- changing config-entry or storage formats.

## Phase 0 — Establish behavioral guardrails

### Work

Before moving code, identify existing tests covering these invariants:

| Invariant | Existing primary coverage |
|---|---|
| Frame checksum and message IDs | `tests/test_protocol.py` |
| LED commands and schedules | `tests/test_client.py` |
| Dosing commands and retry safety | `tests/test_scripted_transport.py`, `tests/test_dosing_protocol.py` |
| Stirrer behavior | `tests/test_mag_stirrer.py`, `tests/test_stirrer_ha.py` |
| Heater behavior | `tests/test_heater.py`, `tests/test_heater_ha.py` |
| Fan/VIVID III behavior | `tests/test_fan.py` |
| Factory detection | `tests/test_factory.py` |
| Connection lifecycle and retries | `tests/test_scripted_transport.py` |
| HA integration setup | `tests/test_home_assistant_integration.py` |
| Vendored package parity | `tests/test_sync_vendor.py` |

Add a regression test only if one of these observable contracts is genuinely uncovered. Do not add tests that assert filenames, inheritance, internal field locations, or import structure.

### Baseline verification

```bash
uv --cache-dir .uv-cache run --group dev pytest
uv --cache-dir .uv-cache run python scripts/sync_vendor.py --check
```

### Exit criteria

- Current suite passes.
- Every transport and device behavior being moved has observable coverage.
- No architecture-only tests added.

## Phase 1 — Extract an injectable BLE transport

This removes Bluetooth lifecycle complexity from the later device split.

### Files

```diff
 src/chihiros_led_control/
 ├── client.py
+├── transport.py
 ├── testing.py
 └── const.py
```

### Add `transport.py`

Define a small transport protocol and real implementation:

```python
class ChihirosTransport(Protocol):
    async def send(
        self,
        frames: Sequence[bytes],
        *,
        attempts: int,
        notification_wait: float,
    ) -> None: ...

    async def disconnect(self) -> None: ...

    def update_device(
        self,
        ble_device: BLEDevice,
        advertisement_data: AdvertisementData,
    ) -> None: ...
```

`BleTransport` owns:

- `BleakClientWithServiceCache`;
- characteristic resolution;
- notification subscription;
- connection and operation locks;
- retry/backoff;
- batch pacing;
- unexpected-disconnect handling;
- idle disconnect timer;
- raw frame writes;
- Bluetooth device and advertisement updates.

The device client supplies:

- a raw notification callback;
- a lazy connection-prelude callback;
- already encoded command frames.

The prelude must be lazy so message IDs are not consumed when an existing connection is reused.

### State ownership

Keep outside the transport:

- message ID generation;
- model/profile metadata;
- device-specific notification parsing;
- optimistic heater/fan state;
- command construction;
- family-specific startup decisions.

Remove the heater’s dependency on the transport’s private operation lock. Heater state updates should use a heater-owned state lock and submit complete command batches through the public transport transaction.

### Convert `ScriptedTransport`

Change `ScriptedTransport` to subclass `BleTransport` and supply only a
scripted BLE client for connection setup. Production `BleTransport` must own
retry, locking, notifications, write pacing, and disconnect lifecycle.

The injected driver usage remains:

```python
transport = ScriptedTransport()
device = ChihirosDevice(..., transport=transport)
await device.query_status()
```

The production constructor creates `BleTransport` when no transport is supplied.

### Tests

Update:

- `tests/test_client.py`
- `tests/test_scripted_transport.py`
- family tests that inject `ScriptedTransport`
- `docs/testing-without-hardware.md`

Preserve tests for:

- connection reuse;
- prelude only once per physical connection;
- missing notify characteristic;
- characteristic pairing;
- retry/reconnect;
- no retry for manual dose and timed calibration;
- idle disconnect;
- command batch pacing.

### Verification

```bash
uv --cache-dir .uv-cache run --group dev pytest \
  tests/test_client.py \
  tests/test_scripted_transport.py \
  tests/test_dosing.py \
  tests/test_heater.py

uv --cache-dir .uv-cache run python scripts/sync_vendor.py
uv --cache-dir .uv-cache run python scripts/sync_vendor.py --check
uv --cache-dir .uv-cache run --group dev pytest
```

### Exit criteria

- Production clients default to `BleTransport`.
- Tests use `ScriptedTransport` to replace only BLE connection/client behavior.
- Transport contains no LED, dosing, stirrer, fan, or heater command knowledge.
- Wire output and connection behavior remain unchanged.

## Phase 2 — Introduce typed device profiles and registry

Do this before splitting drivers so the factory and Home Assistant have a stable discriminator.

### Files

```diff
 src/chihiros_led_control/
 ├── models.py
+├── registry.py
 └── factory.py
```

### Model types

Retain `DeviceModel` as the public metadata container, but replace the expanding boolean set with a discriminated specification.

Suggested shape:

```python
class DeviceKind(StrEnum):
    LED = "led"
    DOSING_PUMP = "dosing_pump"
    MAG_STIRRER = "mag_stirrer"
    HEATER = "heater"


class LedProtocol(StrEnum):
    BLE_LED = "BleLed"
    NEW_BLE_LED = "NewBleLed"
    SEA_LED = "SeaLed"


class LedFeature(StrEnum):
    FAN = "fan"
    TEMPERATURE_PROTECTION = "temperature_protection"
    INDICATOR_LED = "indicator_led"


@dataclass(frozen=True)
class LedSpec:
    channels: Mapping[str, int]
    protocol: LedProtocol
    features: frozenset[LedFeature] = frozenset()
    min_fan_speed: int = 0


@dataclass(frozen=True)
class DosingPumpSpec:
    channel_limit: int = 8


@dataclass(frozen=True)
class MagStirrerSpec:
    channel_limit: int = 8


@dataclass(frozen=True)
class HeaterSpec:
    pass


DeviceSpec = LedSpec | DosingPumpSpec | MagStirrerSpec | HeaterSpec


@dataclass(frozen=True)
class DeviceModel:
    name: str
    advertised_codes: tuple[str, ...]
    spec: DeviceSpec
    needs_device_type: bool = False
    fallback: bool = False
```

`DeviceKind` can be derived from `spec`, avoiding duplicated `kind` and `spec` fields that could disagree.

### Registry ownership

Move from `models.py` into `registry.py`:

- all concrete model declarations;
- `SUPPORTED_MODELS`;
- prefix index;
- longest-prefix ordering;
- fallback profiles;
- generic white/RGB/WRGB profiles;
- known unsupported prefix classification.

Keep `models.py` limited to types and common channel definitions.

Retain public function names:

- `detect_model()`
- `resolve_model()`
- `model_for_device_type()`
- `needs_device_type()`

They may delegate to `registry.py`.

### Replace ambiguous booleans

```diff
- model.sea_led_family
+ model.spec.protocol is LedProtocol.SEA_LED

- model.has_fan
+ LedFeature.FAN in model.spec.features

- model.is_vivid3
+ explicit VIVID feature membership

- model.is_heater
+ isinstance(model.spec, HeaterSpec)
```

Do not identify a family using `model.name`.

### Factory dispatch

Replace singleton comparisons:

```diff
-if resolved_model == HEATER:
-    ...
-if resolved_model == MAG_STIRRER:
-    ...
+driver = DRIVER_BY_SPEC_TYPE[type(resolved_model.spec)]
+return driver(...)
```

At this phase, the driver mapping can still point to classes in `devices/`.

### Home Assistant discriminator

Expose a stable `device_kind` property on real and fake devices. Begin migrating setup/service gating to this property instead of:

- `model_name == "Dosing Pump"`;
- `model_name == "Mag Stirrer"`;
- `model.is_heater`;
- empty or nonempty `colors`.

### Tests

Update:

- `tests/test_factory.py`
- `tests/test_manifest_model_codes.py`
- fake roster declarations
- model constructions in tests

Retain behavior assertions for:

- longest-prefix matching;
- unsupported short-prefix collisions;
- fallback generic selection;
- BleLed versus SeaLed protocol selection;
- VIVID III features;
- dosing, stirrer, and heater classification.

Do not add tests merely asserting the dataclass layout.

### Exit criteria

- `DeviceModel` has no `has_fan`, `is_vivid3`, `is_heater`, or `sea_led_family` fields.
- Family detection never compares display names.
- Product name, device kind, LED protocol, and optional features are independent metadata.
- Prefix detection behavior is unchanged.

## Phase 3 — Split concrete family drivers

### Files

```diff
 src/chihiros_led_control/
-├── client.py
+├── devices/
+│   ├── __init__.py
+│   ├── base.py
+│   ├── led.py
+│   ├── dosing.py
+│   ├── stirrer.py
+│   └── heater.py
```

Delete `client.py` after migrating every internal caller. Do not leave a forwarding module.

### `devices/base.py`

Internal `BaseChihirosDevice` owns only:

- transport reference;
- device identity and model;
- message ID generator;
- common callback registration;
- raw-notification dispatch skeleton;
- `disconnect()`;
- BLE device/advertisement updates;
- logging.

It must not expose:

- `colors`;
- brightness;
- schedules;
- fan control;
- dosing;
- stirring;
- heater state.

Provide family hooks:

```python
def _connection_prelude(self) -> Sequence[bytes]: ...
def _parse_notification(self, data: bytes) -> ParsedNotification | None: ...
def _record_notification(self, notification: ParsedNotification) -> None: ...
```

### `devices/led.py`

Move the current public `ChihirosDevice` here:

- channel normalization;
- brightness;
- manual/auto mode;
- schedule periods and curves;
- runtime/schedule notifications;
- fan/VIVID III features and optimistic state.

Keep the exported class name `ChihirosDevice`.

### `devices/dosing.py`

`ChihirosDosingPump(BaseChihirosDevice)` owns:

- dosing auth/status;
- manual dose;
- daily/lifetime notifications;
- schedules and programming;
- calibration;
- channel reset and totals reset;
- dose-delay state.

It does not inherit LED methods.

### `devices/stirrer.py`

`ChihirosMagStirrer(BaseChihirosDevice)` owns:

- stir start/stop;
- pre-run and speed;
- stir schedules;
- master/slave replay operations required by the integration.

Reuse pure dosing frame builders. Do not inherit `ChihirosDosingPump`.

Where both pump and stirrer need the same multi-frame sequence, extract a pure builder such as:

```python
build_channel_programming_frames(...)
```

Avoid a “dosing device mixin” unless substantial stateful behavior is genuinely shared.

### `devices/heater.py`

`ChihirosHeater(BaseChihirosDevice)` owns:

- heater notification state;
- manual and auto setpoints;
- mode switching;
- protection and calibration;
- temperature unit;
- backlight;
- runtime/alarm state;
- optimistic write-only state.

### Imports and exports

Update:

- `src/chihiros_led_control/__init__.py`
- `factory.py`
- `cli.py`
- `testing.py`
- all tests
- Home Assistant runtime imports

Keep the current top-level exports intact.

### Home Assistant type cutover

Replace the single broad `ChihirosClient` protocol with:

```text
BaseChihirosClient
├── LedChihirosClient
├── DosingChihirosClient
├── StirrerChihirosClient
└── HeaterChihirosClient
```

`ChihirosRuntime.client` becomes a union of these surfaces.

Platform setup must gate before accessing a family API:

- `light.py` → LED only;
- `fan.py` → LED with `LedFeature.FAN`;
- heater entity helpers → heater only;
- dosing services → dosing only;
- stirrer services → stirrer only.

Remove capability helpers that compare names. Use `device_kind`, then cast to the matching protocol.

### Tests

Update direct imports across:

- `test_client.py`
- `test_cli.py`
- `test_factory.py`
- `test_dosing.py`
- `test_mag_stirrer.py`
- `test_heater.py`
- `test_fan.py`
- Home Assistant tests

Use a throwaway introspection smoke check—not a permanent test—to confirm that pump, stirrer, and heater instances no longer expose LED operations.

### Exit criteria

- Pump, stirrer, and heater are siblings of the LED driver.
- Non-LED devices do not expose `set_brightness()`, LED schedule methods, or fan methods.
- Stirrer does not inherit dosing-pump state or status behavior.
- Factory, CLI, HA, fakes, and tests use the new modules.
- `client.py` is removed.
- Top-level package exports still work.

## Phase 4 — Split frame, command, and notification codecs

### Files

```diff
 src/chihiros_led_control/
-├── commands.py
-├── protocol.py
+├── protocol/
+│   ├── __init__.py
+│   ├── frame.py
+│   ├── notifications.py
+│   ├── led.py
+│   ├── dosing.py
+│   ├── stirrer.py
+│   └── heater.py
```

### Ownership

#### `protocol/frame.py`

- message ID normalization;
- reserved-byte handling;
- checksum;
- generic frame encoding;
- timestamp encoding.

#### `protocol/notifications.py`

Passive notification value objects and the `ParsedNotification` union:

- runtime;
- fan status;
- schedule points/snapshots;
- dosing totals/daily;
- heater temperature/status.

#### `protocol/led.py`

- brightness;
- manual/auto mode;
- schedule period commands;
- auto-curve encoding;
- fan and VIVID III commands;
- LED/runtime/fan/schedule parsing.

The auto-curve builder accepts `LedProtocol`, not a `sea_led_family` boolean.

#### `protocol/dosing.py`

- dose volume encoding;
- dosing modes and work points;
- pump scheduling;
- calibration;
- totals/status requests;
- shared channel-programming frame builders;
- dosing notifications.

#### `protocol/stirrer.py`

- stirrer duration conversions;
- overlap validation;
- pre-run/speed command;
- temporary run command.

It may import shared dosing record builders.

#### `protocol/heater.py`

- heater constants and validation;
- temperature and power encoding;
- heater commands;
- heater notification parsing;
- alarm bitmap mapping.

### Import rule

```text
protocol/frame.py imports no family module
family protocol modules may import frame.py
device drivers import only their family protocol modules
Home Assistant imports notification data types, never command builders
```

### Remove old modules

The old monolithic codec modules are deleted; do not add forwarding imports.

Update references in:

- CLI;
- tests;
- HA coordinator;
- integration runtime protocols;
- documentation.

### Tests

Move test imports but preserve byte-level assertions. Suggested grouping:

```text
tests/test_protocol.py
  → common frame + LED protocol

tests/test_dosing_protocol.py
  → dosing protocol

tests/test_heater.py
  → heater protocol and driver

tests/test_mag_stirrer.py
  → stirrer protocol and driver
```

Renaming test files is optional; do not reorganize them solely for symmetry.

### Exit criteria

- Shared frame code contains no family branches.
- No `heater=True` parsing flag remains.
- A family driver imports no unrelated family codec.
- Every existing command-byte assertion remains unchanged.
- Old `commands.py` and `protocol.py` are removed.

## Phase 5 — Align Home Assistant runtime data and fakes

### Split family-specific runtime state

Current `ChihirosData` carries several mutually exclusive optional fields. Replace it with a typed union:

```python
@dataclass
class ChihirosData:
    title: str
    device: BaseChihirosClient
    coordinator: ChihirosDataUpdateCoordinator


@dataclass
class DosingChihirosData(ChihirosData):
    totals: DosingDailyTotals
    volumes: list[float]
    programming: DosingProgrammingTracker
    calibration: DosingCalibrationTracker


@dataclass
class StirrerChihirosData(ChihirosData):
    channels: list[StirrerChannelState]
    programming: DosingProgrammingTracker
```

Heater and LED entries can use the base record until they acquire meaningful family-specific runtime state.

Update service resolvers to require and return the appropriate data type. Remove checks based on whether an optional tracker happens to be non-`None`.

### Split development fakes

Replace the universal fake with family fakes:

```text
custom_components/chihiros/fake/
├── __init__.py
├── registry.py
├── base.py
├── led.py
├── dosing.py
├── stirrer.py
└── heater.py
```

Each fake implements only its matching HA-facing protocol. `create_fake_device()` dispatches from the fake profile’s `DeviceKind`.

Keep the fake roster and fake addresses unchanged so local HA configuration remains stable.

### Simplify integration setup

Reduce `custom_components/chihiros/__init__.py` to:

- setup/unload;
- runtime-data construction;
- coordinator lifecycle;
- service registration lifecycle.

Tests should import service validation helpers from their owning modules instead of relying on the large `__all__` re-export surface in the integration root.

Service presence checks use family data/device kinds:

```diff
- any(data.device.colors ...)
+ any(data.device.device_kind is DeviceKind.LED ...)

- any(data.dosing_totals ...)
+ any(isinstance(data, DosingChihirosData) ...)
```

### Preserve HA contracts

Do not change:

- service names or schemas;
- unique IDs;
- entity names;
- entity registry defaults;
- config entry data;
- dosing/stirrer storage keys;
- fake Bluetooth addresses.

### Tests

Primary coverage:

- `test_home_assistant_unit.py`
- `test_home_assistant_integration.py`
- `test_light_rgb.py`
- `test_heater_ha.py`
- `test_stirrer_ha.py`
- `test_master_slave_ha.py`
- `test_sensor.py`
- `test_coordinator.py`

### Exit criteria

- No universal fake implements every family method.
- `ChihirosData` no longer carries mutually exclusive dosing and stirrer fields.
- Integration setup contains no display-name capability checks.
- Existing HA entities, services, storage, and fake-device behavior remain unchanged.

## Phase 6 — Documentation, vendoring, and final cleanup

### Documentation updates

Update:

- `docs/architecture.md`
  - registry → driver → codec → transport flow;
  - family boundaries;
  - source/vendor ownership.
- `docs/protocol.md`
  - replace references to the family codec package;
  - identify family codec paths.
- `docs/testing-without-hardware.md`
  - injected scripted transport example;
  - new device import paths;
  - family fake layout.
- README examples that import old modules.

Add a shallow architecture diagram:

```text
src/chihiros_led_control/
├── devices/       # domain behavior and family state
├── protocol/      # pure byte encoding and parsing
├── registry.py    # advertised name → metadata
├── factory.py     # metadata → family driver
└── transport.py   # BLE connection lifecycle
```

Do not add a new changelog unless the project adopts one separately.

### Remove obsolete code

Confirm removal of:

- `client.py`;
- `commands.py`;
- monolithic `protocol.py`;
- global transport monkey-patching;
- model-name capability checks;
- old boolean profile fields;
- compatibility re-export modules;
- obsolete comments naming removed paths.

### Vendor synchronization

The existing sync script recursively copies new packages, so `devices/` and `protocol/` require no special sync logic. `testing.py` remains excluded as intended.

Run after every source phase that affects HA, and once more at completion:

```bash
uv --cache-dir .uv-cache run python scripts/sync_vendor.py
uv --cache-dir .uv-cache run python scripts/sync_vendor.py --check
```

### Final verification

```bash
uv --cache-dir .uv-cache run --group dev pytest

uv --cache-dir .uv-cache run python scripts/sync_vendor.py --check

uv --cache-dir .uv-cache run chihirosctl --help

uv --cache-dir .uv-cache run --group dev pre-commit run --all-files
```

Also run scripted smoke scenarios using the real family drivers:

1. LED status query and brightness transaction.
2. VIVID III fan command and notification.
3. Dosing manual dose with one-attempt failure semantics.
4. Stirrer schedule and master/slave frame replay.
5. Heater status query and manual setpoint batch.

## Suggested PR sequence

| PR | Scope | Main risk |
|---|---|---|
| 1 | Injectable `BleTransport` with scripted BLE client adapter | Connection/prelude/retry regressions |
| 2 | Typed profile specifications and registry | Detection or capability regression |
| 3 | Family driver split plus HA type cutover | Missing callers and invalid family assumptions |
| 4 | Family protocol package | Changed command bytes or parsing |
| 5 | Typed HA runtime data and family fakes | Entity/service/storage regression |
| 6 | Documentation and final cleanup | Stale paths or vendored package |

Each PR should be mergeable and fully green. Do not combine protocol behavior changes or new device support with these refactors.

## Final acceptance criteria

- `ChihirosDevice` is an LED client, not the shared base for every device.
- Pump, stirrer, and heater expose only valid family operations.
- Stirrer and dosing share codecs without domain inheritance.
- The registry distinguishes product identity, family, protocol, channels, and features.
- No device family is inferred from a display name.
- Transport contains no device semantics.
- Protocol framing contains no family semantics.
- Family codecs contain no BLE connection logic.
- Home Assistant consumes narrow family protocols.
- Scripted tests inject transport without global monkey-patching.
- Fake devices are family-specific.
- Command bytes, notifications, retries, HA entities, services, and storage remain behaviorally unchanged.
- Source and vendored packages are synchronized.
