"""Workflow definition storage.

CRUD for workflow definitions with versioning. Definitions are
stored in the persistence backend and referenced by workflow runs
via immutable snapshots.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from agents.workflow.definition import WorkflowDefinition

logger = logging.getLogger(__name__)


class StoredDefinition(BaseModel):
    """A versioned workflow definition stored in the backend.

    Attributes:
        name: Workflow name (unique identifier).
        version: Auto-incrementing version number.
        definition: The full workflow definition.
        created_at: When this version was created.
        active: Whether this definition is available for new runs.
    """

    name: str
    version: int = 1
    definition: WorkflowDefinition
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    active: bool = True


class DefinitionStore:
    """Workflow definition store with versioning.

    When initialized with a shared persistence backend, definitions
    are stored as JSON in the workflow state table with a special
    'definition:' prefix. This makes definitions visible across
    all runner replicas.
    """

    def __init__(self, persistence: Any = None) -> None:
        """Initialize the store.

        Args:
            persistence: Optional shared persistence backend. If None,
                uses process-local in-memory storage.
        """
        self._definitions: dict[str, StoredDefinition] = {}
        self._versions: dict[str, list[StoredDefinition]] = {}
        self._persistence = persistence

    async def save(self, definition: WorkflowDefinition) -> StoredDefinition:
        """Save a workflow definition, creating a new version.

        Args:
            definition: The workflow definition to store.

        Returns:
            The stored definition with version number.
        """
        name = definition.metadata["name"]
        versions = self._versions.setdefault(name, [])
        version = len(versions) + 1

        stored = StoredDefinition(
            name=name,
            version=version,
            definition=definition,
        )
        versions.append(stored)
        self._definitions[name] = stored

        if self._persistence:
            from agents.workflow.state import WorkflowState
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            state = WorkflowState(
                workflow_id=f"def:{name}:v{version}",
                workflow_name=f"definition:{name}",
                status="completed",
                definition_snapshot=stored.model_dump(mode="json"),
                created_at=now, updated_at=now,
            )
            await self._persistence.save(state)

        logger.info("Stored workflow definition '%s' v%d", name, version)
        return stored

    async def get(self, name: str) -> Optional[StoredDefinition]:
        """Get the latest active version of a definition.

        Args:
            name: Workflow name.

        Returns:
            Latest StoredDefinition, or None if not found.
        """
        stored = self._definitions.get(name)
        if stored and not stored.active:
            return None
        return stored

    async def get_version(self, name: str, version: int) -> Optional[StoredDefinition]:
        """Get a specific version of a definition.

        Args:
            name: Workflow name.
            version: Version number.

        Returns:
            StoredDefinition at that version, or None.
        """
        versions = self._versions.get(name, [])
        if version < 1 or version > len(versions):
            return None
        return versions[version - 1]

    async def list_all(self) -> list[StoredDefinition]:
        """List all active definitions (latest version of each)."""
        return [d for d in self._definitions.values() if d.active]

    async def delete(self, name: str) -> bool:
        """Soft-delete a definition (mark inactive).

        Args:
            name: Workflow name to delete.

        Returns:
            True if deleted, False if not found.
        """
        stored = self._definitions.get(name)
        if not stored:
            return False
        stored.active = False
        logger.info("Soft-deleted workflow definition '%s'", name)
        return True
