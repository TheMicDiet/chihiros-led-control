"""Tests for Home Assistant manifest requirements."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
MANIFEST = REPO_ROOT / "custom_components" / "chihiros" / "manifest.json"
HOME_ASSISTANT_REQUIREMENT_NAMES = {
    "bleak-retry-connector",
}


def _requirement_name(requirement: str) -> str:
    """Return the normalized package name from a requirement string."""
    name = re.split(r"\s*(?:\[|<|>|=|!|~|;)", requirement, maxsplit=1)[0]
    return name.lower().replace("_", "-")


def test_home_assistant_manifest_requirements_match_project_dependencies() -> None:
    """Home Assistant runtime requirement versions stay aligned with pyproject."""
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project_dependencies = {
        _requirement_name(requirement): requirement for requirement in pyproject["project"]["dependencies"]
    }

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest_requirements = {_requirement_name(requirement): requirement for requirement in manifest["requirements"]}

    expected_requirements = {name: project_dependencies[name] for name in HOME_ASSISTANT_REQUIREMENT_NAMES}
    assert manifest_requirements == expected_requirements
