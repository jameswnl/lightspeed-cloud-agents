"""Graph state and dependency types for pydantic-graph executor.

Bridges our WorkflowState into pydantic-graph's StateT/DepsT type system.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from agents.registry import AgentRegistry
from agents.spawner.base import AgentSpawner
from agents.workflow.advisory import AdvisoryEnforcer
from agents.workflow.auto_approve import ApprovalPolicy
from agents.workflow.persistence import WorkflowPersistence
from agents.workflow.state import WorkflowState


@dataclass
class GraphWorkflowState:
    """pydantic-graph StateT — mutable accumulator for step results.

    Wraps our existing WorkflowState so both executors produce
    the same output shape.
    """

    workflow_state: WorkflowState


@dataclass
class GraphWorkflowDeps:
    """pydantic-graph DepsT — injected dependencies for step functions.

    Provides access to the agent registry, spawner, and other
    infrastructure needed by step functions during execution.
    """

    registry: AgentRegistry
    client_factory: Callable
    spawner: Optional[AgentSpawner] = None
    agent_image: str = "agent-runtime:latest"
    advisory: AdvisoryEnforcer = field(default_factory=lambda: AdvisoryEnforcer())
    approval_policy: ApprovalPolicy = field(default_factory=ApprovalPolicy)
    persistence: Optional[WorkflowPersistence] = None
    event_callback: Optional[Callable] = None
