"""Shared fixtures for the home-agent test suite."""
from __future__ import annotations

import pytest

from home_agent.services.voice_system_context import SystemContext


@pytest.fixture
def ctx() -> SystemContext:
    """Fresh SystemContext for each test."""
    return SystemContext()
