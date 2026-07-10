"""Tests for Home Assistant manifest requirements."""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "custom_components" / "chihiros" / "manifest.json"
HOME_ASSISTANT_PROVIDED_REQUIREMENT_NAMES = {
    "bleak-retry-connector",
}


def _requirement_name(requirement: str) -> str:
    """Return the normalized package name from a requirement string."""
    name = re.split(r"\s*(?:\[|<|>|=|!|~|;)", requirement, maxsplit=1)[0]
    return name.lower().replace("_", "-")


def test_manifest_does_not_duplicate_home_assistant_requirements() -> None:
    """Home Assistant-provided requirements are not pinned by the integration."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest_requirement_names = {_requirement_name(requirement) for requirement in manifest["requirements"]}

    assert manifest_requirement_names.isdisjoint(HOME_ASSISTANT_PROVIDED_REQUIREMENT_NAMES)
