"""Shared fixtures for workflow tests."""

from __future__ import annotations

import logging

import pytest


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """Save and restore root logger state between tests.

    Prevents JSON formatter pollution from configure_logging() tests
    affecting caplog assertions in audit, escalation, and interpolation tests.
    """
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    yield
    root.handlers = original_handlers
    root.setLevel(original_level)
