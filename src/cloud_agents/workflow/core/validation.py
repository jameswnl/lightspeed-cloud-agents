"""Workflow definition validation.

Catches errors at submission time rather than deep in workflow execution.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from cloud_agents.workflow.security.content_policy import ContentPolicy, evaluate_content_policy
from cloud_agents.workflow.core.execution import (
    apply_one_step_defaults,
    inference_spec_from_provider_config,
    reject_secret_bearing_mcp,
    validate_credential_reference,
    validate_inference_provider,
)


def _validate_schema(
    schema: dict[str, Any],
    step_name: str,
    path: str,
    errors: list[str],
) -> None:
    """Recursively validate a JSON Schema fragment.

    Checks that array types have an 'items' definition.
    """
    schema_type = schema.get("type")

    if schema_type == "array" and "items" not in schema:
        errors.append(
            f"output_schema for step '{step_name}': "
            f"'{path}' is type 'array' but missing required 'items' definition"
        )

    if "items" in schema and isinstance(schema["items"], dict):
        _validate_schema(schema["items"], step_name, f"{path}.items", errors)

    # Note: does not recurse into additionalProperties or allOf/anyOf/oneOf.
    for prop_name, prop_schema in schema.get("properties", {}).items():
        if isinstance(prop_schema, dict):
            _validate_schema(prop_schema, step_name, f"{path}.{prop_name}", errors)


def validate_definition(
    defn: dict[str, Any],
    content_policy: Optional[ContentPolicy] = None,
) -> list[str]:
    """Validate a workflow definition dict.

    Parameters:
        defn: The workflow definition dict to validate.
        content_policy: Optional content policy to enforce. When provided,
            the definition is also checked against the policy rules.

    Returns:
        A list of error messages. Empty list means valid.
    """
    errors: list[str] = []
    spec = defn.get("spec", {})
    steps = spec.get("steps", [])
    if not steps:
        errors.append("Workflow must have at least one step")
        return errors

    # One-step convention (issue #268): a single step may omit
    # ``name``/``output_key``; default them so the documented shorthand
    # passes the 422 gate and every downstream check sees the canonical
    # names, matching both runners (#270).
    if len(steps) == 1:
        steps = [apply_one_step_defaults(dict(steps[0]), step_count=1)]

    # Definition-level provider gate (#270): validate the catalog name
    # and reject secret *values* at submission, before any persistence
    # or workflow start, so raw tokens cannot enter serialized run state.
    definition_provider = defn.get("provider")
    if isinstance(definition_provider, dict):
        try:
            # ``credentials_secret`` is a legitimate reference on the
            # legacy ProviderConfig shape; validate the name/model
            # selection and the reference separately.
            inference_spec_from_provider_config(definition_provider)
        except ValueError as exc:
            errors.append(f"Definition provider: {exc}")
        credentials_secret = definition_provider.get("credentials_secret")
        if credentials_secret is not None:
            try:
                validate_credential_reference(credentials_secret)
            except ValueError as exc:
                errors.append(f"Definition provider: {exc}")

    # Workflow-level MCP catalog default (issue #268): steps without
    # their own ``mcp_servers`` inherit this value at normalization, so
    # the secret gate below must check the same merged view.
    workflow_mcp_default = spec.get("mcp_servers")

    output_keys: set[str] = set()
    step_names: set[str] = set()

    for i, step in enumerate(steps):
        name = step.get("name")
        if not name:
            errors.append(f"Step {i} is missing required field 'name'")
            continue

        if name in step_names:
            errors.append(f"Duplicate step name: '{name}'")
        step_names.add(name)

        output_key = step.get("output_key")
        if not output_key:
            errors.append(f"Step '{name}' is missing required field 'output_key'")
        elif output_key in output_keys:
            errors.append(f"Duplicate output_key: '{output_key}' in step '{name}'")
        else:
            output_keys.add(output_key)

        prompt = step.get("prompt") or ""
        refs = re.findall(r"\{\{\s*steps\.(\w+)\.", prompt)
        for ref in refs:
            if ref not in output_keys:
                errors.append(f"Step '{name}' references undefined step '{ref}' in prompt template")

        output_schema = step.get("output_schema")
        if output_schema and isinstance(output_schema, dict):
            _validate_schema(output_schema, name, "root", errors)

        # Mirrors OpenShellSpawner._validate_allowed_skills() -- this copy
        # catches malformed names at submission time (422) instead of only
        # at spawn time; keep both in sync if the rules change.
        allowed_skills = step.get("allowed_skills")
        if allowed_skills:
            for skill_name in allowed_skills:
                if not skill_name:
                    errors.append(f"Step '{name}': allowed_skills entries must not be empty")
                elif "/" in skill_name:
                    errors.append(
                        f"Step '{name}': allowed_skills entries must not contain "
                        f"'/': {skill_name!r}"
                    )
                elif skill_name in (".", ".."):
                    errors.append(
                        f"Step '{name}': allowed_skills entries must not be "
                        f"'.' or '..': {skill_name!r}"
                    )

        # Validate mcp_servers entries at submission time (422) so /run
        # and /definitions agree on the field this PR adds. Without this,
        # an inline dict with a typo (e.g. {"nam": "x", "url": "..."}) is
        # accepted by validate_definition but rejected by
        # WorkflowDefinition.model_validate(), and at runtime the resolver
        # passes it through to server["name"] -> opaque KeyError.
        # Keep in sync with WorkflowStepSpec.mcp_servers typing.
        mcp_servers = step.get("mcp_servers")
        if mcp_servers:
            for j, entry in enumerate(mcp_servers):
                if isinstance(entry, str):
                    if not entry:
                        errors.append(
                            f"Step '{name}': mcp_servers string entries must not be empty"
                        )
                elif isinstance(entry, dict):
                    if not entry.get("name") or not entry.get("url"):
                        errors.append(
                            f"Step '{name}': inline mcp_servers entry [{j}] requires non-empty 'name' and 'url'"
                        )
                else:
                    errors.append(
                        f"Step '{name}': mcp_servers entries must be strings or inline configs"
                    )

        # Canonical secret/provider checks (issue #268): fail fast at
        # submission (422) with the same rules the runners enforce, so
        # both engines and all API surfaces agree. The secret gate runs
        # on the MERGED value (step + workflow-level spec default) so a
        # secret-bearing catalog at spec level is a 422 here, not a
        # runtime failed step.
        try:
            reject_secret_bearing_mcp(step.get("mcp_servers", workflow_mcp_default))
        except ValueError as exc:
            errors.append(f"Step '{name}': {exc}")
        step_provider = step.get("inference_provider") or step.get("provider")
        if step_provider is not None:
            try:
                validate_inference_provider(dict(step_provider))
            except ValueError as exc:
                errors.append(f"Step '{name}': {exc}")

    # --- Content policy checks ---
    if content_policy is not None:
        violations = evaluate_content_policy(defn, content_policy)
        for v in violations:
            errors.append(
                f"Content policy violation ({v.rule}) in step '{v.step_name}': {v.reason}"
            )

    return errors
