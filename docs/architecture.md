# Architecture

`src/chihiros_led_control/` is the source of truth for the reusable Python
library and CLI. Home Assistant code lives in `custom_components/chihiros/` and
acts as an adapter around a vendored runtime copy of that library.

The vendored package at
`custom_components/chihiros/vendor/chihiros_led_control/` exists so HACS
installs remain self-contained. Do not edit vendored files directly. Make
library changes in `src/chihiros_led_control/`, then run:

```bash
uv run python scripts/sync_vendor.py
```

CI checks the copy with:

```bash
uv run python scripts/sync_vendor.py --check
```
