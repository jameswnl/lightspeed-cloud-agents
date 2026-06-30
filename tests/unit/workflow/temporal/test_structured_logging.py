"""Unit tests for structured JSON logging configuration."""

from __future__ import annotations

import json
import logging
import os
from unittest.mock import patch

import pytest

from cloud_agents.workflow.structured_logging import configure_logging


class TestStructuredLogging:
    """Tests for LOG_FORMAT-based logging configuration."""

    def test_json_format_produces_valid_json(
        self, capfd: pytest.CaptureFixture
    ) -> None:
        """LOG_FORMAT=json produces parseable JSON with renamed fields."""
        with patch.dict(os.environ, {"LOG_FORMAT": "json"}):
            configure_logging()
            test_logger = logging.getLogger("test.json_format")
            test_logger.setLevel(logging.INFO)
            test_logger.info("test message")

            captured = capfd.readouterr()
            for line in captured.err.strip().split("\n"):
                if "test message" in line:
                    data = json.loads(line)
                    assert data["message"] == "test message"
                    assert "timestamp" in data
                    assert "level" in data
                    assert "logger" in data
                    assert "asctime" not in data
                    assert "levelname" not in data
                    return
            pytest.fail("Expected JSON log line with 'test message' not found")

    def test_text_format_is_human_readable(self, capfd: pytest.CaptureFixture) -> None:
        """LOG_FORMAT=text (default) produces human-readable output."""
        with patch.dict(os.environ, {"LOG_FORMAT": "text"}):
            configure_logging()
            test_logger = logging.getLogger("test.text_format")
            test_logger.setLevel(logging.INFO)
            test_logger.info("readable message")

            captured = capfd.readouterr()
            assert "readable message" in captured.err

    def test_text_format_is_not_json(self, capfd: pytest.CaptureFixture) -> None:
        """LOG_FORMAT=text output is not valid JSON."""
        with patch.dict(os.environ, {"LOG_FORMAT": "text"}):
            configure_logging()
            test_logger = logging.getLogger("test.not_json")
            test_logger.setLevel(logging.INFO)
            test_logger.info("not json message")

            captured = capfd.readouterr()
            assert "not json message" in captured.err
            for line in captured.err.strip().split("\n"):
                if "not json message" in line:
                    with pytest.raises(json.JSONDecodeError):
                        json.loads(line)
                    return

    def test_default_format_is_text(self, capfd: pytest.CaptureFixture) -> None:
        """No LOG_FORMAT env var defaults to text (not JSON)."""
        with patch.dict(os.environ, {}, clear=False):
            env = os.environ.copy()
            env.pop("LOG_FORMAT", None)
            with patch.dict(os.environ, env, clear=True):
                configure_logging()
                test_logger = logging.getLogger("test.default_format")
                test_logger.setLevel(logging.INFO)
                test_logger.info("default message")

                captured = capfd.readouterr()
                assert "default message" in captured.err
                for line in captured.err.strip().split("\n"):
                    if "default message" in line:
                        with pytest.raises(json.JSONDecodeError):
                            json.loads(line)
                        return
