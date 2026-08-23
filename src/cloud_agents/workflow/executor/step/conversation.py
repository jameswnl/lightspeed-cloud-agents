"""Framework-agnostic conversation message format.

This is OUR format -- stable across framework changes. Adapters convert
to/from framework types (pydantic-ai ModelMessage, etc.) at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


@dataclass
class ConversationMessage:
    """A single message in a conversation.

    Attributes:
        role: Message role.
        content: Message content.
        timestamp: When this message was created.
        metadata: Additional context (tool_name, tool_args, model, tokens).
    """

    role: Literal["user", "assistant", "tool_call", "tool_result"]
    content: str
    timestamp: datetime = field(default_factory=lambda: datetime.now())
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict.

        Returns:
            Dict with role, content, timestamp (ISO string), and metadata.
        """
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversationMessage:
        """Deserialize from a dict.

        Parameters:
            data: Dict with role, content, and optional timestamp/metadata.

        Returns:
            ConversationMessage instance.
        """
        timestamp = data.get("timestamp")
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)
        elif timestamp is None:
            timestamp = datetime.now()

        return cls(
            role=data["role"],
            content=data["content"],
            timestamp=timestamp,
            metadata=data.get("metadata", {}),
        )
