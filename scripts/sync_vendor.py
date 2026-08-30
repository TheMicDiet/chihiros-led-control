#!/usr/bin/env python3
"""Sync the source library into the Home Assistant vendored package."""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "src" / "chihiros_led_control"
VENDOR = REPO_ROOT / "custom_components" / "chihiros" / "vendor" / "chihiros_led_control"

EXCLUDED_DIRS = {
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "tests",
    "dist",
    "build",
    # Future cli/ package of the plugin architecture; cli.py is excluded by name.
    "cli",
}
EXCLUDED_SUFFIXES = {
    ".pyc",
    ".pyo",
}
EXCLUDED_NAMES = {
    "cli.py",
    # Scripted-transport test harness; the HA integration does not use it.
    "testing.py",
    "PKG-INFO",
}


def _is_excluded(path: Path) -> bool:
    """Return whether a path should be excluded from the vendored package."""
    if path.name in EXCLUDED_NAMES:
        return True
    if path.suffix in EXCLUDED_SUFFIXES:
        return True
    return any(part in EXCLUDED_DIRS or part.endswith(".egg-info") for part in path.parts)


def _copy_runtime_tree() -> None:
    """Copy source package files to the vendored package."""
    if VENDOR.exists():
        shutil.rmtree(VENDOR)
    for source_path in SOURCE.rglob("*"):
        relative_path = source_path.relative_to(SOURCE)
        if _is_excluded(relative_path):
            continue
        target_path = VENDOR / relative_path
        if source_path.is_dir():
            target_path.mkdir(parents=True, exist_ok=True)
        else:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)


def _missing_or_extra(comparison: filecmp.dircmp) -> list[str]:
    """Return names present on only one side of the comparison."""
    differences: list[str] = []
    for name in comparison.left_only:
        if not _is_excluded(Path(name)):
            differences.append(f"missing from vendor: {name}")
    for name in comparison.right_only:
        if not _is_excluded(Path(name)):
            differences.append(f"extra in vendor: {name}")
    return differences


def _changed_files(left: Path, right: Path, comparison: filecmp.dircmp) -> list[str]:
    """Return common files whose contents differ."""
    return [
        f"changed: {name}"
        for name in comparison.common_files
        if not _is_excluded(Path(name)) and not filecmp.cmp(left / name, right / name, shallow=False)
    ]


def _compare_entries(left: Path, right: Path, comparison: filecmp.dircmp) -> list[str]:
    """Return differences for files and directories of one comparison level."""
    return [*_missing_or_extra(comparison), *_changed_files(left, right, comparison)]


def _compare_dirs(left: Path, right: Path) -> list[str]:
    """Return a list of differences between two directories."""
    if not right.exists():
        return [f"missing directory: {right.relative_to(REPO_ROOT)}"]

    differences: list[str] = []
    comparison = filecmp.dircmp(left, right, ignore=sorted(EXCLUDED_DIRS))
    differences.extend(_compare_entries(left, right, comparison))
    for name in comparison.subdirs:
        if _is_excluded(Path(name)):
            continue
        sub_differences = _compare_dirs(left / name, right / name)
        differences.extend(str(Path(name) / difference) for difference in sub_differences)
    return differences


def _check_runtime_tree() -> int:
    """Check that the vendored package matches the source package."""
    differences = _compare_dirs(SOURCE, VENDOR)
    if not differences:
        print("Vendored chihiros_led_control package is up to date.")
        return 0

    print("Vendored chihiros_led_control package is stale:")
    for difference in differences:
        print(f"  - {difference}")
    print("Run `python scripts/sync_vendor.py` to update it.")
    return 1


def main() -> int:
    """Run the vendor sync command."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the vendored package does not match src",
    )
    args = parser.parse_args()

    if args.check:
        return _check_runtime_tree()

    _copy_runtime_tree()
    print(f"Synced {SOURCE.relative_to(REPO_ROOT)} to {VENDOR.relative_to(REPO_ROOT)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
