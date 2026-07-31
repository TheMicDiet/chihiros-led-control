"""Test configuration for chihiros-led-control tests."""

from __future__ import annotations

import asyncio

import pytest


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "integration: tests that load the integration through Home Assistant")
    config.addinivalue_line("markers", "unit: isolated tests that do not load a Home Assistant config entry")


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
