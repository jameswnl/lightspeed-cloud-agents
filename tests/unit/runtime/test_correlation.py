"""Unit tests for correlation ID validation."""

import uuid

from agents.runtime.correlation import validate_correlation_id


class TestValidateCorrelationId:
    """Tests for validate_correlation_id."""

    def test_none_generates_uuid(self) -> None:
        """Test that None generates a valid UUID."""
        result = validate_correlation_id(None)
        uuid.UUID(result)

    def test_valid_id_passes_through(self) -> None:
        """Test that a valid correlation ID is returned as-is."""
        assert validate_correlation_id("abc-123-def") == "abc-123-def"

    def test_too_long_generates_uuid(self) -> None:
        """Test that an overly long ID is replaced with a UUID."""
        long_id = "a" * 200
        result = validate_correlation_id(long_id)
        assert result != long_id
        uuid.UUID(result)

    def test_invalid_chars_generates_uuid(self) -> None:
        """Test that IDs with invalid characters are replaced."""
        result = validate_correlation_id("abc;DROP TABLE")
        assert "DROP" not in result
        uuid.UUID(result)

    def test_empty_string_generates_uuid(self) -> None:
        """Test that empty string is replaced with UUID."""
        result = validate_correlation_id("")
        uuid.UUID(result)

    def test_alphanumeric_with_dashes_accepted(self) -> None:
        """Test that alphanumeric + dashes pattern is accepted."""
        test_id = "request-abc-123-XYZ"
        assert validate_correlation_id(test_id) == test_id
