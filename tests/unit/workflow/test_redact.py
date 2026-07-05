"""Unit tests for secret redaction utility (TDD)."""

from __future__ import annotations

import pytest

from cloud_agents.workflow.redact import redact_secrets


class TestRedactSecrets:
    """Tests for redact_secrets utility function."""

    def test_redacts_single_secret(self) -> None:
        """Single secret value is replaced with ***REDACTED***."""
        text = "Error: invalid key sk-abc123xyz"
        result = redact_secrets(text, {"sk-abc123xyz"})
        assert "sk-abc123xyz" not in result
        assert "***REDACTED***" in result

    def test_redacts_multiple_secrets(self) -> None:
        """Multiple distinct secret values are all redacted."""
        text = "keys: sk-abc123 and anthropic-key-456"
        result = redact_secrets(text, {"sk-abc123", "anthropic-key-456"})
        assert "sk-abc123" not in result
        assert "anthropic-key-456" not in result
        assert result.count("***REDACTED***") == 2

    def test_handles_empty_secret_set(self) -> None:
        """Empty secret set returns text unchanged."""
        text = "Error: something went wrong"
        result = redact_secrets(text, set())
        assert result == text

    def test_handles_empty_text(self) -> None:
        """Empty text returns empty string."""
        result = redact_secrets("", {"sk-abc123"})
        assert result == ""

    def test_handles_none_values_in_set(self) -> None:
        """None or empty values in secret set are safely skipped."""
        text = "Error: key sk-abc123 is invalid"
        result = redact_secrets(text, {"sk-abc123", "", None})  # type: ignore[arg-type]
        assert "sk-abc123" not in result
        assert "***REDACTED***" in result

    def test_handles_secret_as_substring(self) -> None:
        """Secret that is a substring of another value is redacted."""
        text = "Error: Authorization Bearer sk-abc123 failed"
        result = redact_secrets(text, {"sk-abc123"})
        assert "sk-abc123" not in result
        assert "***REDACTED***" in result

    def test_overlapping_secrets_both_redacted(self) -> None:
        """Overlapping secrets: longer one redacted first preserves correctness."""
        text = "keys: sk-abc123-extended and sk-abc123"
        result = redact_secrets(text, {"sk-abc123-extended", "sk-abc123"})
        assert "sk-abc123" not in result

    def test_secret_not_present_returns_unchanged(self) -> None:
        """Secret not in text returns text unchanged."""
        text = "Error: timeout waiting for pod"
        result = redact_secrets(text, {"sk-abc123"})
        assert result == text

    def test_preserves_non_secret_content(self) -> None:
        """Non-secret parts of the text are preserved."""
        text = "Failed for step 'diag': key sk-abc123 rejected by provider"
        result = redact_secrets(text, {"sk-abc123"})
        assert "Failed for step 'diag':" in result
        assert "rejected by provider" in result
        assert "sk-abc123" not in result

    def test_multiple_occurrences_all_redacted(self) -> None:
        """All occurrences of a secret value are redacted."""
        text = "key=sk-abc123, retry with key=sk-abc123"
        result = redact_secrets(text, {"sk-abc123"})
        assert "sk-abc123" not in result
        assert result.count("***REDACTED***") == 2
