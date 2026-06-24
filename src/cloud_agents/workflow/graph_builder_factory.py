"""Dynamic graph builder — translates WorkflowDefinition to pydantic-graph.

Constructs a GraphBuilder graph from a YAML-driven WorkflowDefinition
at load time. Each workflow step becomes a graph node.
"""

from __future__ import annotations

import logging
import warnings
from typing import Any

from agents.workflow.definition import WorkflowDefinition, WorkflowStepSpec
from agents.workflow.graph_state import GraphWorkflowDeps, GraphWorkflowState

logger = logging.getLogger(__name__)

warnings.filterwarnings("ignore", category=DeprecationWarning, module="pydantic_graph")

from pydantic_graph import GraphBuilder, StepContext


def build_workflow_graph(
    definition: WorkflowDefinition,
) -> Any:
    """Build a pydantic-graph Graph from a WorkflowDefinition.

    Translates the YAML-driven step list into graph nodes with
    proper edge connections.

    Args:
        definition: The workflow definition from YAML.

    Returns:
        A pydantic-graph Graph ready for execution.
    """
    from agents.workflow.graph_steps import make_agent_step_fn, make_approval_step_fn

    steps = definition.spec.steps
    workflow_name = definition.metadata.get("name", "unnamed")

    gb = GraphBuilder(
        name=workflow_name,
        state_type=GraphWorkflowState,
        deps_type=GraphWorkflowDeps,
        input_type=str,
        output_type=dict,
    )

    step_nodes = []
    for step_spec in steps:
        if step_spec.type == "agent":
            fn = make_agent_step_fn(step_spec)
        elif step_spec.type == "human-approval":
            fn = make_approval_step_fn(step_spec)
        else:
            raise ValueError(f"Unknown step type: {step_spec.type}")

        node = gb.step(fn, node_id=step_spec.name)
        step_nodes.append(node)

    if not step_nodes:
        raise ValueError("Workflow must have at least one step")

    gb.add_edge(gb.start_node, step_nodes[0])
    for i in range(len(step_nodes) - 1):
        gb.add_edge(step_nodes[i], step_nodes[i + 1])
    gb.add_edge(step_nodes[-1], gb.end_node)

    return gb.build()
