"""Canonical agent execution specification.

Issue #268: AgentWorkflow is the only public execution abstraction. A
standalone agent invocation becomes a one-step workflow. This module defines
the single execution-level contract that both one-step and multi-step
workflow steps normalize to before execution:

```text
WorkflowStepSpec
        ↓
normalize and validate
        ↓
AgentExecutionSpec + orchestration metadata
        ↓
StepInput
```

Current model mapping (refactored, not parallel):

- ``WorkflowStepSpec`` execution fields (``prompt``, ``instructions``,
  ``tools``, ``mcp_servers``, ``allowed_skills``, ``permissions``,
  ``spawn``, ``spawn_config``, ``output_schema``, ``context``,
  ``timeout_seconds``) → ``AgentExecutionSpec``.
- ``WorkflowStepSpec`` orchestration fields (``name``, ``output_key``,
  ``condition``, ``max_retries``, ``parallel_group``, ``role``) →
  ``WorkflowAgentStep`` metadata.
- ``ProviderConfig`` → ``InferenceProviderSpec`` (see
  ``inference_spec_from_provider_config``; ``credentials_secret`` is
  dropped, never carried).
- ``SandboxStepInput`` / ``StepInput`` → normalized executor input (see
  ``build_step_input``).
- ``spawn_config.sandbox_image`` → the approved sandbox image used by
  the selected executor (step override wins over the run default).
- ``agent`` → preserved named-agent reference in orchestration metadata;
  never silently dropped, never executed (no registry lookup).
- ``type`` → workflow-step kind; only ``agent`` steps use
  ``AgentExecutionSpec`` (``human-approval`` passes through as metadata).
- ``risk_level`` → retained policy/audit metadata; never widens
  permissions.
- ``service_account`` / ``target_namespaces`` → collapsed with
  ``permissions`` via ``collapse_permissions`` (conflicts rejected;
  namespaces preserved as metadata for content-policy enforcement).
- ``runtime`` → legacy metadata (``spawn`` is the public
  execution/isolation choice selecting Direct/Subprocess/Sandbox
  executors via the ``StepExecutor`` interface).

``StepExecutor`` is the internal interface; ``DirectExecutor``,
``SubprocessExecutor``, and ``SandboxExecutor`` are the implementations
selected by the normalized ``spawn`` value.

Transcript shape: one-step and multi-step executions share the workflow
transcript path. The canonical per-step event shape is
``TranscriptEvent`` with ``normalize_transcript_events`` (see
``core.models``), reused by the tracing/transcript middleware and both
runners -- no separate standalone transcript store or response-only
transcript path exists.

Security invariant: the execution specification must contain no secret
*value*. Pre-#269 carve-out, stated explicitly: the run-level
``credentials_secret`` *reference* (a secret name, validated by
``validate_credential_reference``) still flows to the executor so the
existing ``ensure_credentials_env`` path keeps working -- references are
names, never values, and the full secret-store/injection enforcement is
tracked in #269. Runtime credential *values* remain separate from all
serializable workflow data. Unknown fields are rejected so unexpected
secret-bearing keys fail loudly at validation time instead of being
silently carried through.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable, Literal, Optional, TypeVar
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt

from cloud_agents.spawner.base import SpawnConfig
from cloud_agents.workflow.core.interpolation import interpolate
from cloud_agents.workflow.core.mcp_resolver import resolve_mcp_servers
from cloud_agents.workflow.core.models import MCPServerConfig
from cloud_agents.workflow.core.permissions import PermissionScope
from cloud_agents.workflow.core.state import StepResult as CoreStepResult
from cloud_agents.workflow.core.state import WorkflowState
from cloud_agents.workflow.executor.step.base import StepInput, StepMetadata
from cloud_agents.workflow.executor.step.provider import SUPPORTED_PROVIDER_NAMES

logger = logging.getLogger(__name__)

#: Documented one-step workflow convention: step name for a single-step
#: definition that omits it. Clients must not invent their own names.
ONE_STEP_NAME = "agent"

#: Documented one-step workflow convention: output key for a single-step
#: definition that omits it.
ONE_STEP_OUTPUT_KEY = "result"


class InferenceProviderSpec(BaseModel):
    """LLM/inference provider selection for an agent step.

    Specifically the LLM/inference provider. MCP servers, execution
    backends, and identity providers remain separate types -- do not
    introduce a generic provider hierarchy here.

    Attributes:
        name: Approved provider-profile/catalog key (not an arbitrary
            free-form provider or environment selection; the catalog is
            enforced stack-side).
        model: Model identifier.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    model: str = Field(min_length=1)


class AgentExecutionSpec(BaseModel):
    """One execution-level specification for an agent step.

    Shared by one-step workflows and steps embedded in multi-step
    workflows. Workflow-only orchestration concepts (output_key,
    condition, max_retries, parallel_group, dependencies, human approval
    steps, workflow-level context) live on WorkflowAgentStep, not here.

    List-field merge rule: None means inherit from the workflow/default
    level; an explicit empty list means explicitly empty.

    Attributes:
        prompt: Agent prompt.
        instructions: Optional system/instruction prompt.
        inference_provider: Optional step-level provider override (only
            effective after stack-side authorization and policy validation).
        tools: Tool names this step may use.
        mcp_servers: MCP servers by catalog name or inline config. Inline
            configs must not carry secret-bearing headers, credentialed
            URLs, or physical secret references.
        allowed_skills: Optional skill-name allowlist.
        permissions: Optional permission scope.
        spawn: Execution/isolation choice (public contract).
        spawn_config: Optional spawn configuration.
        output_schema: Optional JSON Schema for structured output.
        context: Step-level context values.
        timeout_seconds: Positive per-step timeout.
    """

    model_config = ConfigDict(extra="forbid")

    prompt: str
    instructions: Optional[str] = None
    inference_provider: Optional[InferenceProviderSpec] = None
    tools: list[str] = Field(default_factory=list)
    mcp_servers: Optional[list[str | MCPServerConfig]] = None
    allowed_skills: Optional[list[str]] = None
    permissions: Optional[PermissionScope] = None
    spawn: Literal["none", "local", "ephemeral"] = "ephemeral"
    spawn_config: Optional[SpawnConfig] = None
    output_schema: Optional[dict[str, Any]] = None
    context: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: Optional[PositiveInt] = None


class WorkflowAgentStep(AgentExecutionSpec):
    """A workflow agent step: execution spec plus orchestration fields.

    Attributes:
        name: Unique step identifier within the workflow.
        output_key: Key in workflow state for this step's output.
        condition: Optional expression -- skip step if false. Evaluated by
            the existing workflow engine, not a new ad hoc evaluator.
        max_retries: Retries after the initial attempt (0 means one total
            attempt). Only explicitly retryable transient failures may be
            retried; side-effecting tool calls, approval denials,
            validation failures, and policy denials are not retried unless
            an idempotency/policy contract allows it.
        parallel_group: Steps in the same group may start concurrently
            after their dependencies and conditions are satisfied. The
            group completes only after all members complete.
        role: Advisory metadata for tracing, audit, and policy
            classification. Does not grant permissions or alter the
            executor by itself.
    """

    name: str
    output_key: str
    condition: Optional[str] = None
    max_retries: NonNegativeInt = 0
    parallel_group: Optional[str] = None
    role: Optional[Literal["analysis", "execution", "verification"]] = None


def apply_one_step_defaults(
    step: dict[str, Any],
    step_count: int = 1,
) -> dict[str, Any]:
    """Apply the documented one-step naming convention to a step dict.

    A single-step definition may omit ``name`` and ``output_key``; they
    default to ``agent`` and ``result``. Multi-step definitions must name
    every step explicitly so clients cannot collide with the convention.

    Parameters:
        step: Raw step dict (not modified).
        step_count: Number of steps in the enclosing definition.

    Returns:
        A new dict with ``name`` and ``output_key`` filled in.

    Raises:
        ValueError: If names are missing and this is not a one-step
            definition.
    """
    result = dict(step)
    if result.get("type") is None:
        result["type"] = "agent"
    if result.get("name") is None or result.get("output_key") is None:
        if step_count != 1:
            raise ValueError(
                "one-step defaults apply to single-step definitions only; "
                "multi-step definitions must set 'name' and 'output_key' "
                "explicitly"
            )
        if result.get("name") is None:
            result["name"] = ONE_STEP_NAME
        if result.get("output_key") is None:
            result["output_key"] = ONE_STEP_OUTPUT_KEY
    return result


#: Substrings marking an infrastructure failure that is safe to retry.
#: Matched case-insensitively against the step error text.
TRANSIENT_FAILURE_MARKERS: tuple[str, ...] = (
    "timeout",
    "timed out",
    "connection",
    "rate limit",
    "429",
    "502",
    "503",
    "504",
    "unavailable",
    "temporarily",
    "spawn failed",
    "readiness",
    "heartbeat",
)

#: Substrings marking a failure that must never be retried. Checked first
#: so an error mentioning both (e.g. a policy denial describing a
#: connection) is treated as non-retryable.
NON_RETRYABLE_MARKERS: tuple[str, ...] = (
    "approval",
    "denied",
    "denies",
    "policy",
    "validation",
    "output_schema",
    "unauthorized",
    "forbidden",
    "401",
    "403",
    "not found",
    "unknown tool",
)

#: Pattern for tool-execution failures. Side-effecting tool calls are not
#: retried unless an idempotency/policy contract allows it -- and no such
#: contract exists yet -- so a failure attributed to a tool invocation is
#: terminal even when its text also mentions infrastructure (e.g. a tool
#: reporting "connection refused"). Conservative by design (S1).
TOOL_FAILURE_PATTERN = re.compile(r"\btool \S+ (failed|error|timed out)\b")

_T = TypeVar("_T")


def is_transient_failure(error: Optional[str]) -> bool:
    """Classify a step failure as retryable-transient or not.

    Parameters:
        error: Step error text (result error or exception message).

    Returns:
        True only when the text matches a transient marker and no
        non-retryable marker. Missing or empty errors are not transient.
    """
    if not error:
        return False
    lowered = error.lower()
    if any(marker in lowered for marker in NON_RETRYABLE_MARKERS):
        return False
    if TOOL_FAILURE_PATTERN.search(lowered):
        return False
    if "tool" in lowered and any(
        marker in lowered for marker in ("failed", "failure", "error", "timed out")
    ):
        return False
    return any(marker in lowered for marker in TRANSIENT_FAILURE_MARKERS)


def max_attempts_for(max_retries: int) -> int:
    """Convert max_retries (retries after the initial attempt) to attempts.

    Parameters:
        max_retries: Non-negative retry count.

    Returns:
        Total attempts including the initial one.
    """
    return max_retries + 1


async def run_with_retries(
    attempt: Callable[[], Awaitable[_T]],
    max_retries: int,
    succeeded: Callable[[_T], bool],
    error_of: Callable[[_T], Optional[str]],
    exc_text: Callable[[BaseException], str] | None = None,
    retry_sleep: Callable[[float], Awaitable[None]] | None = None,
    backoff_seconds: float = 1.0,
) -> _T:
    """Run an attempt callable with unified retry semantics.

    Shared by the local and Temporal runners so one-step and multi-step
    steps retry identically: only transient failures (per
    is_transient_failure) are retried, up to max_retries after the
    initial attempt. Non-transient results return immediately;
    non-transient exceptions propagate immediately; exhausted transient
    exceptions re-raise the last one.

    Parameters:
        attempt: Zero-argument async callable performing one attempt.
        max_retries: Retries after the initial attempt (0 = try once).
        succeeded: Predicate over a returned result.
        error_of: Extracts the error text from a returned result.
        exc_text: Extracts the classification text from a raised
            exception (defaults to ``str``). The Temporal runner passes
            an unwrapping extractor so ``ActivityError`` wrappers are
            classified by their root cause, not the wrapper text.
        retry_sleep: Optional async sleep function used between retries.
        backoff_seconds: Initial exponential backoff delay.

    Returns:
        The first successful result, the last non-transient result, or
        the last result when attempts are exhausted.

    Raises:
        Exception: The non-transient exception, or the last transient
            exception once attempts are exhausted.
    """
    text_of = exc_text or str
    attempts = max_attempts_for(max_retries)
    last_exc: Optional[BaseException] = None
    for index in range(attempts):
        try:
            result = await attempt()
        except Exception as exc:  # noqa: BLE001 -- classified below
            if index + 1 >= attempts or not is_transient_failure(text_of(exc)):
                raise
            last_exc = exc
            if retry_sleep is not None:
                await retry_sleep(backoff_seconds * (2**index))
            continue
        if succeeded(result):
            return result
        if index + 1 >= attempts or not is_transient_failure(error_of(result)):
            return result
        if retry_sleep is not None:
            await retry_sleep(backoff_seconds * (2**index))
    assert last_exc is not None  # noqa: S101 -- loop always runs >= once
    raise last_exc


def activity_error_text(exc: BaseException) -> str:
    """Extract classification text from a (possibly wrapped) exception.

    Walks the ``cause`` / ``__cause__`` chain so Temporal
    ``ActivityError`` wrappers -- whose own text names only the activity
    and retry state -- classify by their root failure (e.g. a 502 from
    the sandbox). Bounded to five levels.

    Parameters:
        exc: The raised exception.

    Returns:
        Chained exception texts joined for classification.
    """
    parts: list[str] = []
    current: BaseException | None = exc
    for _ in range(5):
        if current is None:
            break
        text = str(current)
        if text:
            parts.append(text)
        nxt = getattr(current, "cause", None) or current.__cause__
        current = nxt if isinstance(nxt, BaseException) else None
    return " | ".join(parts)


def chunk_parallel_groups(
    steps: list[dict[str, Any]],
) -> list[tuple[str | None, list[dict[str, Any]]]]:
    """Chunk ordered steps into sequential units and parallel groups.

    Contiguous steps sharing a ``parallel_group`` form one unit that the
    Temporal runner executes concurrently (``asyncio.gather``); ungrouped
    steps form singleton units. A group ends where the group name changes
    -- the same name appearing later starts a new unit.

    Parameters:
        steps: Ordered raw step mappings.

    Returns:
        List of ``(group_name, members)`` tuples in order; ``group_name``
        is None for singleton sequential steps.
    """
    chunks: list[tuple[str | None, list[dict[str, Any]]]] = []
    index = 0
    while index < len(steps):
        group = steps[index].get("parallel_group")
        if group:
            members = []
            while index < len(steps) and steps[index].get("parallel_group") == group:
                members.append(steps[index])
                index += 1
            member_names = {
                identifier
                for member in members
                for identifier in (member.get("name"), member.get("output_key"))
                if identifier
            }
            has_internal_dependency = any(
                (
                    set(
                        re.findall(
                            r"(?:\{\{\s*)?steps\.(\w+)",
                            (member.get("prompt") or "")
                            + " "
                            + (member.get("condition") or ""),
                        )
                    )
                    & member_names
                )
                for member in members
            )
            if has_internal_dependency:
                chunks.extend((None, [member]) for member in members)
            else:
                chunks.append((group, members))
        else:
            chunks.append((None, [steps[index]]))
            index += 1
    return chunks


# ---------------------------------------------------------------------------
# Normalization: WorkflowStepSpec dict → WorkflowAgentStep → StepInput
#
# Canonical execution path (issue #268): one-step and multi-step workflows
# use this identical path. Precedence for inherited configuration is::
#
#     service defaults
#             ↓
#     workflow defaults
#             ↓
#     step-level overrides
#
# ``None`` means inherit; an explicit empty list means explicitly empty.
#
# Documented semantics applied here:
#
# - Context merge is shallow: step values override workflow values key by
#   key; nested dicts are replaced, not deep-merged.
# - ``timeout_seconds`` is a positive per-step timeout (each step's own
#   budget, not a whole-workflow budget). When neither the step nor the
#   workflow sets one, both runners apply the same effective default of
#   600 (local ``StepInput`` construction and the Temporal fallback
#   agree); the ``WorkflowStepSpec`` model default of 3600 applies to
#   validated definitions that carry the key explicitly.
# - ``output_schema`` is validated structurally at submission time (see
#   ``validation.validate_definition``); at runtime a step whose output
#   fails schema validation fails the step (non-retryable validation
#   failure) rather than retrying.
# - ``condition`` uses the existing workflow engine's expression language
#   (see ``core.conditions.evaluate_condition``); no ad hoc evaluator is
#   introduced here. Conditions are evaluated by the runners, not by
#   normalization -- normalization only carries the expression through.
# - ``max_retries`` counts retries after the initial attempt (0 means one
#   total attempt); only transient failures retry (see
#   ``is_transient_failure``). Note the intended default change from 1
#   to 0 versus older definitions: existing workflows relying on one
#   default retry must now set ``max_retries: 1`` explicitly.
#   Normalization failure maps to a failed step, never a crashed run.
# - Retries wrap the middleware stack on the local runner, so each
#   attempt re-emits tracing spans and re-saves transcripts
#   (attempt-numbered spans are future work).
# - ``parallel_group``: eligible steps in the same group may start
#   concurrently after their dependencies and conditions are satisfied
#   (Temporal runner: ``asyncio.gather`` over contiguous same-group
#   steps; local runner: sequential with a warning until group
#   scheduling lands there). The group completes only after all members
#   complete; a failed/denied member stops subsequent steps but does not
#   cancel siblings already running, and there is no implicit
#   concurrency limit. ``role`` is advisory metadata for tracing,
#   audit, and policy classification; it does not grant permissions or
#   alter the executor by itself.
# - Step names must be unique within a workflow; ``agent``/``result`` are
#   the documented one-step convention (see ``ONE_STEP_NAME``).
# ---------------------------------------------------------------------------

#: Provider names the executors can actually run. Aliases
#: ``executor.step.provider.SUPPORTED_PROVIDER_NAMES`` (single source of
#: truth): validation here means "the executor knows this name", while
#: tenant authorization against the approved catalog is enforced
#: stack-side (cloud-agents validates structure; it is not the tenant
#: authorization boundary).
APPROVED_INFERENCE_PROVIDERS: frozenset[str] = SUPPORTED_PROVIDER_NAMES

#: Substrings marking an MCP mapping key as secret-bearing. Inline MCP
#: configs carrying these keys must pass explicit policy validation
#: before execution; normalization rejects them outright.
MCP_SECRET_KEY_MARKERS: tuple[str, ...] = (
    "secret",
    "credentials_secret",
    "api_key",
    "apikey",
    "token",
    "password",
    "authorization",
)

#: Header names that must never appear as plain-text inline MCP headers.
#: ``Authorization``/``Proxy-Authorization`` values are credentials by
#: definition; ``Cookie``/``Set-Cookie`` carry session secrets.
MCP_FORBIDDEN_PLAINTEXT_HEADERS: frozenset[str] = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
)

#: Pattern a credential *reference* must match: an env var key or a K8s
#: secret name (letters, digits, ``_``/``-``/``.``). Values with spaces or
#: other characters are secret values, not references, and are rejected.
CREDENTIAL_REFERENCE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,253}")

#: Prefixes marking a string as a secret *value*, never a reference.
#: References carrying these fail validation instead of flowing to the
#: credential-resolution path.
SECRET_VALUE_PREFIXES: tuple[str, ...] = (
    "sk-",
    "bearer ",
    "akia",
    "xoxb-",
    "xoxp-",
    "xoxa-",
    "xoxr-",
    "xoxs-",
    "ghp_",
    "gho_",
    "ghs_",
    "-----begin",
)


def validate_credential_reference(ref: Any) -> str:
    """Validate a ``credentials_secret`` reference (name, never a value).

    The pre-#269 execution path resolves this to an env var key, so an
    arbitrary caller-chosen value here would be resolved as a credential
    source. References must look like env var keys or K8s secret names;
    known secret-value shapes are rejected outright. Full secret-store
    enforcement is tracked in #269.

    Parameters:
        ref: The candidate reference.

    Returns:
        The validated reference unchanged.

    Raises:
        ValueError: If the reference is not a well-formed name.
    """
    if not isinstance(ref, str) or not ref:
        raise ValueError(f"credentials_secret must be a secret name, got {ref!r}")
    if len(ref) > 254 or CREDENTIAL_REFERENCE_PATTERN.fullmatch(ref) is None:
        raise ValueError(
            f"credentials_secret {ref!r} is not a valid secret name "
            "(letters, digits, '_', '-', '.')"
        )
    lowered = ref.lower()
    if any(lowered.startswith(prefix) for prefix in SECRET_VALUE_PREFIXES):
        raise ValueError(
            f"credentials_secret {ref!r} looks like a secret value, not a "
            "secret name; runtime credential values must never enter "
            "workflow data (#269)"
        )
    return ref


def validate_inference_provider(raw: dict[str, Any]) -> InferenceProviderSpec:
    """Validate a provider mapping as an approved inference provider.

    Parameters:
        raw: Mapping with ``name`` and ``model`` keys.

    Returns:
        The validated ``InferenceProviderSpec``.

    Raises:
        ValueError: If the provider name is not an approved catalog key.
    """
    spec = InferenceProviderSpec.model_validate(raw)
    if spec.name not in APPROVED_INFERENCE_PROVIDERS:
        raise ValueError(
            f"unapproved inference provider {spec.name!r}: must be one of "
            f"{sorted(APPROVED_INFERENCE_PROVIDERS)} (approved catalog key, "
            "enforced stack-side)"
        )
    return spec


def enforce_provider_boundary(
    selected_provider: Optional[InferenceProviderSpec],
    run_provider: dict[str, Any],
) -> None:
    """Reject provider overrides that cross the run authorization boundary.

    Cloud-agents validates provider structure, but the stack owns tenant
    catalog authorization. Until an explicit authorized-provider set is
    passed through that boundary, allowing a nested definition provider to
    differ from the run provider could select a worker's ambient credentials
    (for example Azure credentials during an OpenAI run).

    Parameters:
        selected_provider: Effective workflow/step provider, if any.
        run_provider: Provider selected and authorized for the run.

    Raises:
        ValueError: If no run provider is available or the providers differ.
    """
    if selected_provider is None:
        return
    run_name = run_provider.get("name")
    if not run_name:
        raise ValueError(
            "provider override requires an authorized run provider; "
            f"selected {selected_provider.name!r} without a run boundary"
        )
    if selected_provider.name != run_name:
        raise ValueError(
            "cross-provider override is not authorized: run provider "
            f"{run_name!r}, selected provider {selected_provider.name!r}; "
            "the stack must authorize the provider before execution"
        )


def inference_spec_from_provider_config(
    raw: dict[str, Any],
) -> InferenceProviderSpec:
    """Map a legacy ``ProviderConfig`` mapping to ``InferenceProviderSpec``.

    Drops ``credentials_secret``: runtime credentials are supplied through
    the runtime context/injection contract (tracked in #269), never through
    the serializable execution spec.

    Mapping:

    - ``ProviderConfig.name`` → ``InferenceProviderSpec.name`` (validated
      against the approved catalog)
    - ``ProviderConfig.model`` → ``InferenceProviderSpec.model``
    - ``ProviderConfig.credentials_secret`` → dropped (not carried)
    - ``ProviderConfig.model_provider`` → dropped (sandbox-pod override,
      not part of the inference selection contract)

    Parameters:
        raw: Provider mapping, possibly with ``credentials_secret``.

    Returns:
        The validated ``InferenceProviderSpec`` without credentials.

    Raises:
        ValueError: If the provider name is not approved.
    """
    allowed_keys = {"name", "model", "credentials_secret", "model_provider"}
    unknown_keys = set(raw) - allowed_keys
    if unknown_keys:
        raise ValueError(
            "unknown provider fields: " + ", ".join(sorted(unknown_keys))
        )
    return validate_inference_provider({"name": raw.get("name"), "model": raw.get("model")})


def _is_secret_key(key: str) -> bool:
    """Check whether a mapping key looks secret-bearing."""
    lowered = key.lower()
    return any(marker in lowered for marker in MCP_SECRET_KEY_MARKERS)


def _as_mapping(value: Any) -> dict[str, Any]:
    """Coerce a provider/config value to a plain mapping.

    Accepts raw dicts (YAML path) and pydantic models (validated
    ``WorkflowDefinition`` path) so normalization behaves identically
    whichever shape a definition arrives in.

    Parameters:
        value: Dict or pydantic model.

    Returns:
        A plain dict view of the value.
    """
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return dict(value)


def resolve_sandbox_image(
    spawn_config: Optional[SpawnConfig],
    workflow_defaults: Optional[dict[str, Any]],
    run_image: Optional[str],
) -> str:
    """Resolve the sandbox image through the precedence chain (issue #268).

    ``step spawn_config.sandbox_image`` → workflow-definition
    ``spawn_config.sandbox_image`` → run-level default →
    ``sandbox:latest``. Run-level images are defaults only: a workflow
    definition value outranks them, and a step value outranks both.

    Parameters:
        spawn_config: Normalized step-or-default spawn config (may be
            None or carry no image).
        workflow_defaults: Workflow-level defaults (definition layer).
        run_image: Run-level default image.

    Returns:
        The effective sandbox image.
    """
    if spawn_config is not None and spawn_config.sandbox_image:
        return spawn_config.sandbox_image
    defaults_config = _as_mapping(
        (workflow_defaults or {}).get("spawn_config") or {}
    )
    default_image = defaults_config.get("sandbox_image")
    if default_image:
        return default_image
    return run_image or "sandbox:latest"


def reject_secret_bearing_mcp(
    mcp_servers: Optional[list[Any]],
) -> Optional[list[Any]]:
    """Reject inline MCP configs carrying secret values.

    Production callers should use approved named MCP catalog entries
    (plain strings). Inline configs must not carry secret *values*:
    credentialed URLs (embedded ``user:pass@`` userinfo, with or without
    a scheme), plaintext secret-bearing header names, or header *values*
    shaped like credentials. Secret *references* via ``secret_headers``
    carry no values and pass normalization, but they execute only
    through the explicit ``MCP_ALLOWED_SECRETS`` policy gate in the
    activity layer -- unallowlisted references fail closed there. A
    ``credentials_secret`` key inside an MCP entry has no such gate and
    is rejected.

    Parameters:
        mcp_servers: Step ``mcp_servers`` value (strings and/or inline
            mappings), or None.

    Returns:
        The input unchanged when it carries no secret-bearing material.

    Raises:
        ValueError: If any inline entry carries secret values.
    """
    if not mcp_servers:
        return mcp_servers
    for entry in mcp_servers:
        if isinstance(entry, str):
            continue
        data = entry.model_dump() if hasattr(entry, "model_dump") else entry
        if not isinstance(data, dict):
            continue
        name = data.get("name", "?")
        url = data.get("url", "")
        if isinstance(url, str) and url:
            userinfo = urlparse(url).netloc.split("@", 1)
            schemeless = re.match(r"^[\w.%+\-]+:[^@/]+@", url) is not None
            if (len(userinfo) == 2 and userinfo[0]) or schemeless:
                raise ValueError(
                    f"inline mcp_servers entry {name!r} carries a credentialed "
                    "URL (userinfo embedded); use an approved named catalog "
                    "entry instead"
                )
        headers = data.get("headers")
        if isinstance(headers, dict):
            for header_name, header_value in headers.items():
                if str(header_name).lower() in MCP_FORBIDDEN_PLAINTEXT_HEADERS:
                    raise ValueError(
                        f"inline mcp_servers entry {name!r} carries plaintext "
                        f"secret header {header_name!r}; use an approved "
                        "named catalog entry instead"
                    )
                if _is_secret_key(str(header_name)):
                    raise ValueError(
                        f"inline mcp_servers entry {name!r} carries "
                        f"secret-like header {header_name!r}; use an approved "
                        "named catalog entry instead"
                    )
                if isinstance(header_value, str) and _looks_like_secret_value(header_value):
                    raise ValueError(
                        f"inline mcp_servers entry {name!r} carries a "
                        f"credential-shaped value in header {header_name!r}; "
                        "use an approved named catalog entry instead"
                    )
        if data.get("credentials_secret"):
            raise ValueError(
                f"inline mcp_servers entry {name!r} carries "
                "credentials_secret, which has no policy gate; use an "
                "approved named catalog entry instead"
            )
        for key in data:
            if (
                _is_secret_key(str(key))
                and key not in ("secret_headers", "headers", "url", "name")
                and data.get(key)
            ):
                raise ValueError(
                    f"inline mcp_servers entry {name!r} carries secret-bearing "
                    f"key {key!r}; use an approved named catalog entry instead"
                )
    return mcp_servers


def _looks_like_secret_value(value: str) -> bool:
    """Check whether a header value is shaped like a credential.

    Matches known secret prefixes (API keys, bearer tokens, AWS keys,
    chatops tokens, private-key blocks). Benign values (tenant IDs,
    feature flags) pass through.

    Parameters:
        value: The header value to inspect.

    Returns:
        True when the value looks like a credential.
    """
    lowered = value.lower()
    return any(lowered.startswith(prefix) for prefix in SECRET_VALUE_PREFIXES)


def collapse_permissions(
    permissions: Any,
    service_account: Optional[str],
    risk_level: Optional[str],
    target_namespaces: Optional[list[str]] = None,
) -> tuple[Optional[PermissionScope], dict[str, Any]]:
    """Collapse legacy auth fields with the canonical ``PermissionScope``.

    ``permissions`` is the canonical authorization scope. Legacy fields
    combine with it as follows, never widening it:

    - ``service_account`` fills ``permissions.service_account`` only when
      the scope has none; a conflicting value on both sides is rejected
      instead of silently picking one.
    - ``risk_level`` is retained as audit/policy-classification metadata
      only; it never grants permissions or alters the scope.
    - ``target_namespaces`` has no ``PermissionScope`` equivalent field
      and no runner enforces it at execution (pre-existing -- runtime
      namespace enforcement is #269). It is preserved as step metadata
      for the submission-time content-policy ``allowed_namespaces``
      check and context namespacing, not dropped and not merged into
      the scope.

    Parameters:
        permissions: Canonical scope (mapping or ``PermissionScope``), or
            None.
        service_account: Legacy per-step service account, or None.
        risk_level: Legacy risk classification, or None.
        target_namespaces: Legacy namespace constraint, or None.

    Returns:
        Tuple of the effective scope (or None) and a metadata dict with
        the preserved ``risk_level``/``target_namespaces`` values.

    Raises:
        ValueError: On conflicting ``service_account`` values.
    """
    scope: Optional[PermissionScope] = None
    if permissions is not None:
        scope = (
            permissions
            if isinstance(permissions, PermissionScope)
            else PermissionScope.model_validate(permissions)
        )
    if service_account:
        if scope is not None and scope.service_account not in (None, service_account):
            raise ValueError(
                f"conflicting service_account: permissions sets "
                f"{scope.service_account!r} but the step sets "
                f"{service_account!r}; refusing to widen or silently narrow"
            )
        if scope is None:
            scope = PermissionScope(service_account=service_account)
        elif scope.service_account is None:
            scope = scope.model_copy(update={"service_account": service_account})
    meta: dict[str, Any] = {}
    if risk_level is not None:
        meta["risk_level"] = risk_level
    if target_namespaces is not None:
        meta["target_namespaces"] = list(target_namespaces)
    return scope, meta


def merge_context(
    workflow_context: dict[str, Any],
    step_context: dict[str, Any],
) -> dict[str, Any]:
    """Merge workflow-level and step-level context (shallow, step wins).

    Neither input is modified. Nested mappings are replaced wholesale,
    not deep-merged.

    Parameters:
        workflow_context: Workflow-level context values.
        step_context: Step-level context values (win on key conflict).

    Returns:
        A new merged dict.
    """
    merged = dict(workflow_context)
    merged.update(step_context)
    return merged


def normalize_workflow_step(
    step: dict[str, Any],
    workflow_defaults: Optional[dict[str, Any]] = None,
    step_count: int = 1,
) -> tuple[WorkflowAgentStep, dict[str, Any]]:
    """Normalize one raw step dict to the canonical agent-step contract.

    Applies the one-step naming convention, the service → workflow →
    step precedence chain, provider validation, MCP secret rejection, and
    permission collapse. ``None`` inherits the workflow default; an
    explicit empty list means explicitly empty.

    Parameters:
        step: Raw step mapping (not modified).
        workflow_defaults: Workflow-level defaults for ``provider``,
            ``timeout_seconds``, ``spawn``, ``spawn_config``,
            ``mcp_servers``, ``allowed_skills``, ``permissions``, and
            ``context``.
        step_count: Number of steps in the enclosing definition (drives
            the one-step defaulting rule).

    Returns:
        Tuple of the normalized ``WorkflowAgentStep`` and an orchestration
        metadata dict carrying the preserved legacy/workflow fields
        (``type``, ``agent``, ``message``, ``runtime``, ``risk_level``,
        ``target_namespaces``) so none are silently dropped.

    Raises:
        ValueError: On non-agent step types, missing names in multi-step
            definitions, non-positive timeouts, unapproved providers,
            secret-bearing MCP configs, or conflicting permissions.
    """
    defaults = workflow_defaults or {}
    raw = apply_one_step_defaults(dict(step), step_count=step_count)

    step_type = raw.get("type", "agent")
    if step_type == "human-approval":
        raise ValueError(
            "human-approval steps carry no agent execution spec; they pass "
            "through normalization as workflow metadata (see "
            "normalize_definition)"
        )
    if step_type != "agent":
        raise ValueError(
            f"unknown step type {step_type!r}: only 'agent' steps use " "AgentExecutionSpec"
        )

    if not raw.get("name"):
        raise ValueError("agent steps require 'name'")

    timeout = raw.get("timeout_seconds")
    if timeout is None:
        timeout = defaults.get("timeout_seconds")
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
    ):
        raise ValueError(f"timeout_seconds must be a positive per-step timeout, got {timeout!r}")

    provider_raw = raw.get("inference_provider")
    provider_from_defaults = False
    if provider_raw is None:
        provider_raw = raw.get("provider")
    if provider_raw is None:
        provider_raw = defaults.get("provider")
        provider_from_defaults = provider_raw is not None
    inference_provider = None
    if provider_raw is not None:
        provider_mapping = _as_mapping(provider_raw)
        if provider_from_defaults and (
            "credentials_secret" in provider_mapping or "model_provider" in provider_mapping
        ):
            inference_provider = inference_spec_from_provider_config(provider_mapping)
        else:
            inference_provider = validate_inference_provider(provider_mapping)

    mcp_servers = raw.get("mcp_servers")
    if mcp_servers is None:
        mcp_servers = defaults.get("mcp_servers")
    allowed_skills = raw.get("allowed_skills")
    if allowed_skills is None:
        allowed_skills = defaults.get("allowed_skills")
    reject_secret_bearing_mcp(mcp_servers)

    permissions = raw.get("permissions")
    if permissions is None:
        permissions = defaults.get("permissions")
    service_account = raw.get("service_account")
    if service_account is None:
        service_account = defaults.get("service_account")
    scope, perm_meta = collapse_permissions(
        permissions,
        service_account,
        raw.get("risk_level"),
        raw.get("target_namespaces"),
    )

    spawn = raw.get("spawn")
    if spawn is None:
        spawn = defaults.get("spawn", "ephemeral")
    spawn_config_raw = raw.get("spawn_config")
    if spawn_config_raw is None:
        spawn_config_raw = defaults.get("spawn_config")
    spawn_config = None
    if spawn_config_raw is not None:
        spawn_config = (
            spawn_config_raw
            if isinstance(spawn_config_raw, SpawnConfig)
            else SpawnConfig.model_validate(_as_mapping(spawn_config_raw))
        )

    tools = raw.get("tools", [])
    if tools is None:
        tools = []
    if isinstance(tools, str) or not isinstance(tools, list):
        raise ValueError(
            f"tools must be a list of tool names, got {tools!r} "
            "(a bare string would splinter into per-character names)"
        )
    workflow_context = defaults.get("context")
    step_context = raw.get("context")
    if workflow_context is not None and not isinstance(workflow_context, dict):
        raise ValueError("workflow context must be an object")
    if step_context is not None and not isinstance(step_context, dict):
        raise ValueError("step context must be an object")
    context = merge_context(workflow_context or {}, step_context or {})

    normalized = WorkflowAgentStep(
        prompt=raw.get("prompt", ""),
        instructions=raw.get("instructions"),
        inference_provider=inference_provider,
        tools=list(tools),
        mcp_servers=mcp_servers,
        allowed_skills=allowed_skills,
        permissions=scope,
        spawn=spawn,
        spawn_config=spawn_config,
        output_schema=raw.get("output_schema"),
        context=context,
        timeout_seconds=timeout,
        name=raw["name"],
        output_key=raw["output_key"],
        condition=raw.get("condition"),
        max_retries=raw.get("max_retries", 0),
        parallel_group=raw.get("parallel_group"),
        role=raw.get("role"),
    )
    meta: dict[str, Any] = {
        "type": step_type,
        "agent": raw.get("agent"),
        "message": raw.get("message"),
        "runtime": raw.get("runtime"),
    }
    meta.update(perm_meta)
    return normalized, meta


def workflow_defaults_from_definition(
    definition: dict[str, Any],
) -> dict[str, Any]:
    """Extract workflow-level defaults from a definition mapping.

    Collects the top-level ``provider`` default plus ``spec``-level shared
    values (timeout, spawn, spawn_config, service_account, MCP/skills
    catalogs, permissions, context). Both runners build these identically
    so the service → workflow → step precedence chain is real, not
    doc-only.

    Parameters:
        definition: Workflow definition mapping.

    Returns:
        Defaults dict for ``normalize_workflow_step`` (possibly empty).
    """
    spec = definition.get("spec", {})
    workflow_defaults: dict[str, Any] = {}
    if definition.get("provider") is not None:
        workflow_defaults["provider"] = definition["provider"]
    for key in (
        "timeout_seconds",
        "spawn",
        "spawn_config",
        "service_account",
        "mcp_servers",
        "allowed_skills",
        "permissions",
        "context",
    ):
        if spec.get(key) is not None:
            workflow_defaults[key] = spec[key]
    return workflow_defaults


def normalize_definition(
    definition: dict[str, Any],
) -> tuple[list[WorkflowAgentStep], list[dict[str, Any]]]:
    """Normalize every agent step in a workflow definition identically.

    Applies ``normalize_workflow_step`` to each ``agent`` step with the
    definition's workflow-level defaults (top-level ``provider`` plus
    ``spec``-level shared values). ``human-approval`` steps pass through
    as metadata entries (no agent spec). Step names must be unique.

    Parameters:
        definition: Workflow definition mapping with ``spec.steps`` and
            optional top-level ``provider`` workflow default.

    Returns:
        Tuple of normalized agent steps and a parallel metadata list
        (one entry per definition step, in order).

    Raises:
        ValueError: On duplicate step names or invalid steps.
    """
    spec = definition.get("spec", {})
    raw_steps = spec.get("steps", [])
    step_count = len(raw_steps)
    workflow_defaults = workflow_defaults_from_definition(definition)

    # Names must be unique across ALL step kinds (N3): track every name
    # as steps are processed -- explicit names, defaulted one-step names,
    # and approval gates alike. An agent step and an approval step
    # sharing a name fail either way.
    seen: set[str] = set()
    for raw in raw_steps:
        name = raw.get("name")
        if name is not None:
            if name in seen:
                raise ValueError(f"Duplicate step name: '{name}'")
            seen.add(name)

    steps: list[WorkflowAgentStep] = []
    metas: list[dict[str, Any]] = []
    used_names: set[str] = set()
    for raw in raw_steps:
        if raw.get("type") == "human-approval":
            gate = apply_one_step_defaults(dict(raw), step_count=step_count)
            if gate.get("name") in used_names:
                raise ValueError(f"Duplicate step name: '{gate.get('name')}'")
            used_names.add(gate.get("name"))  # type: ignore[arg-type]
            metas.append(
                {
                    "type": "human-approval",
                    "agent": None,
                    "message": gate.get("message"),
                    "runtime": "sandbox",
                    "name": gate.get("name"),
                    "output_key": gate.get("output_key"),
                    "condition": gate.get("condition"),
                }
            )
            continue
        normalized, meta = normalize_workflow_step(
            raw, workflow_defaults=workflow_defaults, step_count=step_count
        )
        if normalized.name in used_names:
            raise ValueError(f"Duplicate step name: '{normalized.name}'")
        used_names.add(normalized.name)
        steps.append(normalized)
        metas.append(meta)
    return steps, metas


def _interpolate_step_text(template: Optional[str], wf_state: WorkflowState) -> Optional[str]:
    """Interpolate step placeholders, failing open like the runners.

    Parameters:
        template: Prompt template, or None.
        wf_state: Workflow state for placeholder resolution.

    Returns:
        The interpolated text, the raw template when unresolvable, or
        None when no template was given.
    """
    if template is None or "{{" not in template:
        return template
    try:
        return interpolate(template, wf_state)
    except ValueError as exc:
        logger.debug("Template interpolation failed for %r: %s", template, exc)
        return template


def build_step_input(
    step: dict[str, Any],
    run_context: dict[str, Any],
    workflow_defaults: Optional[dict[str, Any]] = None,
    step_count: int = 1,
) -> StepInput:
    """Build the canonical ``StepInput`` for one step (issue #268).

    This is the single construction helper both the local
    (pydantic-graph) and Temporal runners use: normalize the raw step,
    interpolate prompt text against prior results, resolve MCP servers
    against the run catalog, and assemble the executor input.

    Credential handling (pre-#269 contract): the ``StepInput`` provider
    carries only the approved ``name``/``model`` selection plus the
    ``credentials_secret`` *reference* (a secret name / env var key, never
    a secret value) so the existing ``ensure_credentials_env`` execution
    path keeps working. Secret *values* must never appear in the
    definition, the normalized spec, or any serialized state,
    checkpoint, transcript, log, or telemetry payload. The full
    secret-store/injection design is tracked in #269.

    Sandbox-image precedence: step ``spawn_config.sandbox_image`` →
    run ``sandbox_image`` default.

    Parameters:
        step: Raw step mapping (not modified).
        run_context: Run-level values: ``provider`` mapping (may include
            ``credentials_secret``, which is stripped), ``sandbox_image``,
            ``skills_image``, ``skills_paths``, ``mcp_servers`` catalog,
            ``workflow_id``, ``step_results`` (prior outputs keyed by
            output_key), ``user_id``, ``session_id``, and optional
            ``tools_module``.
        workflow_defaults: Workflow-level defaults (see
            ``normalize_workflow_step``).
        step_count: Steps in the enclosing definition (one-step rule).

    Returns:
        The normalized ``StepInput`` with ``raw_step`` preserved.

    Raises:
        ValueError: On any normalization failure (see
            ``normalize_workflow_step``).
    """
    normalized, meta = normalize_workflow_step(
        step, workflow_defaults=workflow_defaults, step_count=step_count
    )

    provider_name = ""
    provider_model = ""
    credentials_secret = None
    run_provider = run_context.get("provider") or {}
    if normalized.inference_provider is not None:
        provider_name = normalized.inference_provider.name
        provider_model = normalized.inference_provider.model
        enforce_provider_boundary(normalized.inference_provider, run_provider)
        # A step override selects name/model only. The run-level
        # credential reference is honored solely when it names the same
        # provider: otherwise the override would silently bind another
        # provider's credentials (cross-provider confusion). On mismatch
        # the override provider's default env key resolves instead.
        if provider_name == run_provider.get("name"):
            credentials_secret = run_provider.get("credentials_secret")
        if credentials_secret is None:
            definition_provider = _as_mapping((workflow_defaults or {}).get("provider") or {})
            if definition_provider.get("name") == provider_name:
                credentials_secret = definition_provider.get("credentials_secret")
    else:
        provider_name = run_provider.get("name", "")
        provider_model = run_provider.get("model", "")
        credentials_secret = run_provider.get("credentials_secret")
        if provider_name and provider_name not in APPROVED_INFERENCE_PROVIDERS:
            raise ValueError(
                f"unapproved inference provider {provider_name!r}: must be one "
                f"of {sorted(APPROVED_INFERENCE_PROVIDERS)}"
            )
    if credentials_secret is not None:
        credentials_secret = validate_credential_reference(credentials_secret)

    step_results = run_context.get("step_results") or {}
    # Mirror the local runner's _to_workflow_state: step_results is keyed
    # by output_key, and templates resolve {{ steps.X.output... }} against
    # those same keys.
    wrapped: dict[str, CoreStepResult] = {}
    for key, value in step_results.items():
        if isinstance(value, dict):
            status = value.get("status", "completed")
            if status not in (
                "pending",
                "running",
                "completed",
                "failed",
                "skipped",
                "awaiting_approval",
                "dispatched",
            ):
                status = "completed"
            wrapped[key] = CoreStepResult(
                step_name=key,
                status=status,
                output=value.get("output"),
                error=value.get("error"),
            )
        else:
            wrapped[key] = CoreStepResult(step_name=key, status="completed", output=value)
    wf_state = WorkflowState(
        workflow_id=run_context.get("workflow_id", ""),
        workflow_name="",
        current_step=normalized.name,
        steps=wrapped,
        created_at="",
        updated_at="",
    )

    prompt = _interpolate_step_text(normalized.prompt, wf_state) or ""
    system_prompt = _interpolate_step_text(normalized.instructions, wf_state)

    sandbox_image = resolve_sandbox_image(
        normalized.spawn_config,
        workflow_defaults,
        run_context.get("sandbox_image"),
    )

    provider_payload: dict[str, Any] = {
        "name": provider_name,
        "model": provider_model,
    }
    if credentials_secret:
        # Reference only (secret name / env var key) for the pre-#269
        # ensure_credentials_env execution path -- never a secret value.
        provider_payload["credentials_secret"] = credentials_secret

    normalized_step = normalized.model_dump(exclude_none=True)
    normalized_step.update({key: value for key, value in meta.items() if value is not None})
    normalized_step["prompt"] = prompt
    if system_prompt is not None:
        normalized_step["instructions"] = system_prompt

    return StepInput(
        prompt=prompt,
        provider=provider_payload,
        system_prompt=system_prompt,
        output_schema=normalized.output_schema,
        tools=list(normalized.tools),
        tools_module=run_context.get("tools_module"),
        context=dict(step_results),
        execution_context=dict(normalized.context),
        timeout_seconds=normalized.timeout_seconds or 600,
        sandbox_image=sandbox_image,
        skills_image=run_context.get("skills_image"),
        skills_paths=run_context.get("skills_paths"),
        allowed_skills=normalized.allowed_skills,
        mcp_servers=resolve_mcp_servers(normalized.mcp_servers, run_context.get("mcp_servers")),
        workflow_id=run_context.get("workflow_id", ""),
        raw_step=normalized_step,
        step_name=normalized.name,
        output_key=normalized.output_key,
        metadata=StepMetadata(
            user_id=run_context.get("user_id"),
            session_id=run_context.get("session_id"),
        ),
    )
