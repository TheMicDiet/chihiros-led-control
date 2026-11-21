# Chihiros repository guide

## Code and runtime copies

- `src/chihiros_led_control/` is the source of truth for the BLE drivers (LEDs, dosing pumps, magnetic stirrers, and heaters) and the `chihirosctl` CLI. See [the architecture guide](docs/architecture.md) for family boundaries and state ownership.
- `custom_components/chihiros/` is the Home Assistant integration. It imports the generated runtime copy under `custom_components/chihiros/vendor/chihiros_led_control/` so HACS installs are self-contained. Never edit that copy directly. After changing library runtime code, run both commands and include the generated changes:

  ```sh
  uv --cache-dir .uv-cache run python scripts/sync_vendor.py
  uv --cache-dir .uv-cache run python scripts/sync_vendor.py --check
  ```

  The CLI and the scripted BLE test harness are not vendored.

## Local validation

Always pass `--cache-dir .uv-cache` to `uv`. The repo defaults to Python 3.14, which CI uses for Home Assistant tests. The library supports Python 3.13.2 and later; keep library code compatible with Ruff's `py313` target. Ruff uses a 120-character line limit and a maximum cyclomatic complexity of 5; it excludes the vendor tree.

| Task | Command |
| --- | --- |
| Library tests without Home Assistant | `uv --cache-dir .uv-cache run --group test pytest -m 'not homeassistant'` |
| Home Assistant tests | `uv --cache-dir .uv-cache run --group ha-test pytest -m homeassistant` |
| Full suite | `uv --cache-dir .uv-cache run --group dev pytest` |
| Pre-commit checks | `uv --cache-dir .uv-cache run --group lint pre-commit run --all-files` |
| CLI help | `uv --cache-dir .uv-cache run --extra cli chihirosctl --help` |

Each test needs exactly one layer marker: `unit`, `component`, or `integration`. Mark HA-dependent tests with `homeassistant` in addition to their layer. In HA-dependent test modules, call `pytest.importorskip("homeassistant")` before importing HA-only dependencies, including `voluptuous`; pytest imports modules before applying `-m`. Verify dependency isolation in a fresh environment with only the `test` group; an existing `dev` environment can hide missing imports. Put regressions at the lowest public boundary that reproduces the failure. Use real local code and simulated external hardware; do not add mocks, monkeypatching, or assertions about private calls. See [Testing Without Chihiros Hardware](docs/testing-without-hardware.md#test-boundaries) for examples.

`docker compose up` starts Home Assistant with fake Chihiros devices enabled. See [the Docker guide](docs/home-assistant-docker.md) for testing with real BLE on Linux.
