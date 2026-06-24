"""Workflow state persistence.

Provides in-memory and file-based persistence for workflow state.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from agents.workflow.state import WorkflowState


class WorkflowPersistence(ABC):
    """Abstract interface for workflow state storage."""

    @abstractmethod
    async def save(self, state: WorkflowState) -> None:
        """Save workflow state."""

    @abstractmethod
    async def load(self, workflow_id: str) -> Optional[WorkflowState]:
        """Load workflow state by ID."""

    @abstractmethod
    async def list_active(self) -> list[WorkflowState]:
        """List all active workflows."""

    @abstractmethod
    async def delete(self, workflow_id: str) -> None:
        """Delete workflow state."""


class InMemoryPersistence(WorkflowPersistence):
    """In-memory workflow state storage."""

    def __init__(self) -> None:
        """Initialize empty store."""
        self._store: dict[str, WorkflowState] = {}

    async def save(self, state: WorkflowState) -> None:
        """Save state in memory."""
        self._store[state.workflow_id] = state

    async def save_cas(self, state: WorkflowState, expected_version: int) -> bool:
        """Compare-and-swap save. Returns False if version mismatch."""
        existing = self._store.get(state.workflow_id)
        if existing and existing.version != expected_version:
            return False
        state.version = expected_version + 1
        self._store[state.workflow_id] = state
        return True

    async def load(self, workflow_id: str) -> Optional[WorkflowState]:
        """Load state from memory."""
        return self._store.get(workflow_id)

    async def list_active(self) -> list[WorkflowState]:
        """List all stored workflows."""
        return list(self._store.values())

    async def delete(self, workflow_id: str) -> None:
        """Remove from memory."""
        self._store.pop(workflow_id, None)


class FilePersistence(WorkflowPersistence):
    """File-based workflow state persistence."""

    def __init__(self, state_dir: str = "/app/state") -> None:
        """Initialize with state directory.

        Args:
            state_dir: Directory to store workflow state JSON files.
        """
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    async def save(self, state: WorkflowState) -> None:
        """Save state to a JSON file."""
        path = self._dir / f"{state.workflow_id}.json"
        path.write_text(state.model_dump_json(indent=2))
        os.chmod(path, 0o600)

    async def load(self, workflow_id: str) -> Optional[WorkflowState]:
        """Load state from a JSON file."""
        path = self._dir / f"{workflow_id}.json"
        if not path.exists():
            return None
        return WorkflowState.model_validate_json(path.read_text())

    async def list_active(self) -> list[WorkflowState]:
        """List all workflow states from JSON files."""
        states = []
        for path in self._dir.glob("*.json"):
            try:
                states.append(WorkflowState.model_validate_json(path.read_text()))
            except Exception:
                continue
        return states

    async def save_cas(self, state: WorkflowState, expected_version: int) -> bool:
        """Compare-and-swap save using tempfile + atomic rename."""
        import tempfile

        path = self._dir / f"{state.workflow_id}.json"
        if path.exists():
            current = WorkflowState.model_validate_json(path.read_text())
            if current.version != expected_version:
                return False
        state.version = expected_version + 1
        fd, tmp = tempfile.mkstemp(dir=str(self._dir), suffix=".tmp")
        try:
            os.write(fd, state.model_dump_json(indent=2).encode())
            os.close(fd)
            os.rename(tmp, str(path))
            os.chmod(str(path), 0o600)
        except Exception:
            os.close(fd) if not os.get_inheritable(fd) else None
            Path(tmp).unlink(missing_ok=True)
            raise
        return True

    async def delete(self, workflow_id: str) -> None:
        """Delete the state file."""
        path = self._dir / f"{workflow_id}.json"
        path.unlink(missing_ok=True)
