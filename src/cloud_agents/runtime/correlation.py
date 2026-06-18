"""Correlation ID validation and generation.

Validates caller-provided correlation IDs and generates server-side UUIDs
when absent or invalid. Prevents log injection from untrusted input.
"""

from __future__ import annotations

import re
import uuid

MAX_CORRELATION_ID_LENGTH = 128
VALID_PATTERN = re.compile(r"^[a-zA-Z0-9\-]+$")


def validate_correlation_id(value: str | None) -> str:
    """Validate and normalize a correlation ID.

    Args:
        value: Caller-provided correlation ID, or None.

    Returns:
        A validated correlation ID (original if valid, generated UUID if not).
    """
    if value is None:
        return str(uuid.uuid4())
    if len(value) > MAX_CORRELATION_ID_LENGTH:
        return str(uuid.uuid4())
    if not VALID_PATTERN.match(value):
        return str(uuid.uuid4())
    return value
