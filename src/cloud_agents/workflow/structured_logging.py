"""Structured logging configuration.

Configures JSON or text logging based on LOG_FORMAT env var.
JSON mode uses python-json-logger for centralized log aggregation.
"""

from __future__ import annotations

import logging
import os
import sys


def configure_logging() -> None:
    """Configure root logger based on LOG_FORMAT env var.

    LOG_FORMAT=json: structured JSON output for Loki/ELK/Splunk.
    LOG_FORMAT=text (default): human-readable output for dev.
    """
    log_format = os.environ.get("LOG_FORMAT", "text").lower()
    root = logging.getLogger()

    root.handlers.clear()

    handler = logging.StreamHandler(sys.stderr)

    if log_format == "json":
        from pythonjsonlogger.json import JsonFormatter

        formatter = JsonFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={
                "asctime": "timestamp",
                "levelname": "level",
                "name": "logger",
            },
        )
        handler.setFormatter(formatter)
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s — %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)

    root.addHandler(handler)
    root.setLevel(logging.INFO)
