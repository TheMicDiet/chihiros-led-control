"""Tests that the Home Assistant manifest covers all advertised model codes."""

from __future__ import annotations

import json
from pathlib import Path

from chihiros_led_control.models import SUPPORTED_MODELS

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "custom_components" / "chihiros" / "manifest.json"


def _manifest_local_name_prefixes() -> list[str]:
    """Return the local_name matcher prefixes from the HA manifest."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return [matcher["local_name"].removesuffix("*") for matcher in manifest["bluetooth"] if "local_name" in matcher]


def test_manifest_bluetooth_matchers_cover_all_model_codes() -> None:
    """Every advertised model code is discoverable through the manifest."""
    prefixes = _manifest_local_name_prefixes()
    missing = [
        code
        for model in SUPPORTED_MODELS
        for code in model.advertised_codes
        if not any(code.startswith(prefix) or prefix.startswith(code) for prefix in prefixes)
    ]
    assert not missing, f"model codes missing from manifest.json bluetooth matchers: {missing}"


def test_manifest_bluetooth_matchers_follow_app_prefix_discovery() -> None:
    """The manifest matches only the device families this repository supports.

    Home Assistant rejects local name matchers shorter than three literal
    characters, and the vendor app scans by `DY*` prefix. To avoid surfacing
    unsupported Chihiros families (heaters, CO2 controllers, fans, gateways,
    stirrers, power outlets) in discovery, the manifest pins one matcher per
    supported family instead of using broad `DY?*` buckets.
    """
    prefixes = _manifest_local_name_prefixes()
    expected = [
        "DYSSD",
        "DYZSD",
        "DYDD",
        "DYNA2",
        "DYA",
        "DYARGB",
        "DYRGBA",
        "DYNARGB",
        "DYREE",
        "DYRGBV",
        "DYNV",
        "DYSEA",
        "DYONE",
        "DYTWO",
        "DYC",
        "DYCOM",
        "DYNC2",
        "DYNT90",
        "DYWRGB",
        "DYNW",
        "DYWPR",
        "DYSILN",
        "DYSL",
        "DYVVD3",
        "DYNCRGP",
        "DYNCRGB",
        "DYU",
        "DYLED",
        "DYNLED",
        "DYDOSE",
        "DYTDOS",
        "DYNDOS",
    ]
    assert prefixes == expected
    missing = [
        code
        for model in SUPPORTED_MODELS
        for code in model.advertised_codes
        if not any(code.startswith(prefix) for prefix in prefixes)
    ]
    assert not missing, f"model codes missing from manifest.json bluetooth matchers: {missing}"


def test_manifest_bluetooth_matchers_exclude_unsupported_families() -> None:
    """Unsupported Chihiros families are not discovered by name.

    Three prefixes are unavoidably matched because the supported single-letter
    family codes `DYA` and `DYC` require the `DYA*` / `DYC*` matchers:
    ``DYAPRCO2`` (CO2), ``DYCHIL`` (chiller) and ``DYCO2``. Every other known
    unsupported family (fans, heaters, gateways, stirrers, outlets, doctors,
    unsupported lights) must not be matched.
    """
    prefixes = _manifest_local_name_prefixes()
    unavoidable = {"DYAPRCO2", "DYCHIL", "DYCO2"}
    unsupported = (
        "DYAPRCO2",  # CO2 generator
        "DYCHIL",  # chiller
        "DYCO2",  # CO2 controller
        "DYECO",  # eco device
        "DYFAN",  # cooling fan
        "DYGATE",  # gateway
        "DYHET",  # heater
        "DYMIXR",  # magnetic stirrer
        "DYPWR",  # power outlet
        "DYPWSK",  # power socket
        "DYNDOC",  # doctor
        "DYNFAN",  # new-gen cooling fan
        "DYNGATE",  # new-gen gateway
        "DYNSCO2",  # new-gen CO2
        "DYLITE",  # unsupported light
        "DYSET",  # unknown accessory
        "DYTEST",  # test device
    )
    matched = {name for name in unsupported if any(name.startswith(prefix) for prefix in prefixes)}
    assert matched == unavoidable
