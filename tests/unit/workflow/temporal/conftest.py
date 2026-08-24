"""Shared fixtures for temporal workflow tests."""

from __future__ import annotations

import logging

import pytest


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """Save and restore root logger state between tests.

    configure_logging() in test_structured_logging.py sets a JSON formatter
    on the root logger globally. Without cleanup, subsequent tests using caplog
    get JSON-formatted records where r.message contains JSON instead of the
    raw log string, causing false assertion failures.
    """
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    yield
    root.handlers = original_handlers
    root.setLevel(original_level)
