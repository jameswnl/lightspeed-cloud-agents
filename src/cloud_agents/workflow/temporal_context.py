"""Build sandbox request context from workflow step results.

Translates step outputs into the context sections the sandbox expects:
targetNamespaces, previousAttempts, approvedOption, executionResult.
"""

from __future__ import annotations

from typing import Any

from cloud_agents.workflow.temporal_models import StepResult


def build_sandbox_context(
    workflow_steps: dict[str, StepResult],
    current_step: dict[str, Any],
) -> dict[str, Any]:
    """Build sandbox context from workflow state and current step config.

    Parameters:
        workflow_steps: Completed step results keyed by output_key.
        current_step: Current step definition dict from the workflow YAML.

    Returns:
        Context dict with applicable sections for the sandbox request.
    """
    context: dict[str, Any] = {}

    if namespaces := current_step.get("target_namespaces"):
        context["targetNamespaces"] = namespaces

    failed = [
        {"step": key, "error": result.error or "unknown", "output": result.output}
        for key, result in workflow_steps.items()
        if result.status == "failed"
    ]
    if failed:
        context["previousAttempts"] = failed

    if approval_key := current_step.get("approval_step"):
        analysis_key = current_step.get("analysis_step")
        if (
            approval_key in workflow_steps
            and analysis_key
            and analysis_key in workflow_steps
        ):
            approval_output = workflow_steps[approval_key].output or {}
            analysis_output = workflow_steps[analysis_key].output or {}
            selected_id = approval_output.get("selected_option_id")
            options = analysis_output.get("options", [])

            if selected_id:
                approved = next(
                    (o for o in options if o.get("id") == selected_id),
                    options[0] if options else None,
                )
            else:
                approved = options[0] if options else None

            if approved:
                context["approvedOption"] = approved

    if execution_key := current_step.get("execution_step"):
        if execution_key in workflow_steps:
            exec_output = workflow_steps[execution_key].output
            if exec_output:
                context["executionResult"] = exec_output

    return context
