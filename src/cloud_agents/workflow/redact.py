"""Secret redaction utility for logs and error responses.

Provides a function to replace known secret values in text with a
redaction placeholder, preventing credential leaks in error messages,
logs, and API responses.
"""

from __future__ import annotations

REDACTED_PLACEHOLDER = "***REDACTED***"


def redact_secrets(text: str, secret_values: set[str]) -> str:
    """Replace any occurrence of secret values with a redaction placeholder.

    Longer secrets are replaced first to handle cases where one secret
    is a substring of another.

    Parameters:
        text: The text to redact secrets from.
        secret_values: Set of secret values to search for and replace.

    Returns:
        Text with all secret value occurrences replaced by '***REDACTED***'.
    """
    # Sort by length descending so longer secrets are replaced first,
    # preventing partial matches when one secret is a prefix of another.
    sorted_secrets = sorted(
        (v for v in secret_values if v),
        key=len,
        reverse=True,
    )
    for val in sorted_secrets:
        if val in text:
            text = text.replace(val, REDACTED_PLACEHOLDER)
    return text
