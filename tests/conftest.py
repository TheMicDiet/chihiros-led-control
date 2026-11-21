"""Test configuration for chihiros-led-control tests."""

from __future__ import annotations

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Require one explicit pyramid layer before marker-based deselection."""
    invalid = []
    for item in items:
        layers = {mark.name for mark in item.iter_markers() if mark.name in {"unit", "component", "integration"}}
        if len(layers) != 1:
            invalid.append(f"{item.nodeid}: {', '.join(sorted(layers)) or 'unmarked'}")
    if invalid:
        raise pytest.UsageError(
            "Each test must have exactly one layer marker (unit, component, integration):\n" + "\n".join(invalid)
        )
