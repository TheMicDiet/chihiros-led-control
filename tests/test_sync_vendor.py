"""Tests for the vendored library sync script."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SYNC_VENDOR_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_vendor.py"
SYNC_VENDOR_SPEC = importlib.util.spec_from_file_location(
    "sync_vendor", SYNC_VENDOR_PATH
)
assert SYNC_VENDOR_SPEC is not None
assert SYNC_VENDOR_SPEC.loader is not None
sync_vendor = importlib.util.module_from_spec(SYNC_VENDOR_SPEC)
SYNC_VENDOR_SPEC.loader.exec_module(sync_vendor)


def test_sync_vendor_copies_runtime_files_and_check_passes(
    tmp_path, monkeypatch
) -> None:
    """Sync copies runtime files and check mode passes."""
    source = tmp_path / "src" / "chihiros_led_control"
    vendor = (
        tmp_path / "custom_components" / "chihiros" / "vendor" / "chihiros_led_control"
    )
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "cli.py").write_text("CLI_ONLY = True\n", encoding="utf-8")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "ignored.pyc").write_bytes(b"cache")

    monkeypatch.setattr(sync_vendor, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sync_vendor, "SOURCE", source)
    monkeypatch.setattr(sync_vendor, "VENDOR", vendor)

    sync_vendor._copy_runtime_tree()

    assert (vendor / "__init__.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (vendor / "cli.py").exists()
    assert not (vendor / "__pycache__").exists()
    assert sync_vendor._check_runtime_tree() == 0


def test_sync_vendor_check_fails_when_vendor_is_stale(tmp_path, monkeypatch) -> None:
    """Check mode fails when the vendored copy is stale."""
    source = tmp_path / "src" / "chihiros_led_control"
    vendor = (
        tmp_path / "custom_components" / "chihiros" / "vendor" / "chihiros_led_control"
    )
    source.mkdir(parents=True)
    vendor.mkdir(parents=True)
    (source / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (vendor / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")

    monkeypatch.setattr(sync_vendor, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sync_vendor, "SOURCE", source)
    monkeypatch.setattr(sync_vendor, "VENDOR", vendor)

    assert sync_vendor._check_runtime_tree() == 1
