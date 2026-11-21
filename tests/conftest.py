"""Test configuration for chihiros-led-control tests."""

from __future__ import annotations

import asyncio

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


@pytest.fixture(autouse=True)
def enable_event_loop_debug():
    """Enable event loop debug mode (Python 3.10+ compatible).

    Overrides the broken fixture from pytest-homeassistant-custom-component
    which calls asyncio.get_event_loop() and fails on Python 3.14.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        loop.set_debug(True)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.set_debug(True)
