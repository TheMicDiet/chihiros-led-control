"""The vendor-sync command against a temporary on-disk package tree."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.component
SYNC_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_vendor.py"


def test_vendor_sync_and_check(tmp_path: Path) -> None:
    """Sync ships only runtime files; check detects a stale install without rewriting it."""
    script = tmp_path / "scripts" / SYNC_SCRIPT.name
    script.parent.mkdir()
    shutil.copyfile(SYNC_SCRIPT, script)
    source = tmp_path / "src" / "chihiros_led_control"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "cli.py").touch()
    (source / "testing.py").touch()
    vendor = tmp_path / "custom_components" / "chihiros" / "vendor" / "chihiros_led_control"

    command = [sys.executable, str(script)]
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=5)
    assert [file.name for file in vendor.iterdir()] == ["__init__.py"]
    subprocess.run([*command, "--check"], check=True, capture_output=True, text=True, timeout=5)

    (source / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    result = subprocess.run([*command, "--check"], check=False, capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert "changed: __init__.py" in result.stdout
    assert (vendor / "__init__.py").read_text(encoding="utf-8") == "VALUE = 1\n"
