"""Temporal workflow API endpoints.

Provides REST endpoints for starting, approving, querying, and
cancelling Temporal-backed agent workflows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from cloud_agents.workflow.content_policy import ContentPolicy

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from temporalio.client import Client, WorkflowExecutionStatus

from cloud_agents.workflow.audit import emit_audit
from cloud_agents.workflow.definition_store import DefinitionStore
from cloud_agents.workflow.temporal_models import (
    MCPServerConfig,
    ProviderConfig,
    WorkflowInput,
)
from cloud_agents.workflow.temporal_worker import DEFAULT_TASK_QUEUE
from cloud_agents.workflow.temporal_workflow import AgentWorkflow

logger = logging.getLogger(__name__)


def _emit_content_policy_audit(
    workflow_id: str,
    definition: dict[str, Any],
    errors: list[str],
) -> None:
    """Emit an audit event for content policy violations.

    Parameters:
        workflow_id: Workflow run ID or placeholder.
        definition: The rejected workflow definition.
        errors: List of validation error messages.
    """
    policy_errors = [e for e in errors if "content policy" in e.lower()]
    if policy_errors:
        emit_audit(
            event_type="content_policy_violation",
            workflow_id=workflow_id,
            details={
                "definition_name": definition.get("metadata", {}).get("name", ""),
                "violations": policy_errors,
            },
        )


class RunWorkflowRequest(BaseModel):
    """Request body for starting a workflow."""

    workflow_name: str | None = None
    definition: dict[str, Any] | None = None
    input_prompt: str | None = None
    provider: ProviderConfig | None = None
    sandbox_image: str = (
        "quay.io/openshift-lightspeed/lightspeed-agentic-sandbox:latest"
    )
    skills_image: str | None = None
    skills_paths: list[str] | None = None
    advisory: bool | None = None
    workflow_id: str | None = None
    mcp_servers: list[MCPServerConfig] | None = None
    approval_policy: dict[str, Any] | None = None
    notifier_config: dict[str, Any] | None = None
    escalation_config: dict[str, Any] | None = None


class ApproveRequest(BaseModel):
    """Request body for sending an approval signal."""

    step_name: str
    decision: str
    selected_option_id: str | None = None


class SendMessageRequest(BaseModel):
    """Request body for sending a message to a CLI session."""

    message: str


def build_temporal_router(
    temporal_client: Client,
    auth_dependency: Optional[Any] = None,
    authorizer: Optional[Any] = None,
    definition_store: Optional[DefinitionStore] = None,
    content_policy: Optional["ContentPolicy"] = None,
    cli_session_launcher: Optional[Any] = None,
    cli_session_spawner: Optional[Any] = None,
) -> APIRouter:
    """Build FastAPI router with Temporal workflow endpoints.

    Parameters:
        temporal_client: Connected Temporal client instance.
        auth_dependency: Optional FastAPI auth dependency. All endpoints
            require authentication when provided.
        authorizer: Optional WorkflowAuthorizer for fine-grained access
            control. Defaults to NoopAuthorizer (permit all).
        definition_store: Optional store for workflow-name resolution.
        content_policy: Optional content policy for definition validation.
            When provided, definitions are checked against the policy rules
            at both /run and /definitions submission time.
        cli_session_launcher: Optional CLISessionLauncher for CLI session
            management. When provided, CLI session endpoints are registered.
        cli_session_spawner: Optional AgentSpawner for CLI session containers.
            Required when cli_session_launcher is provided.

    Returns:
        APIRouter with workflow endpoints.
    """
    from cloud_agents.workflow.authorization import (
        NoopAuthorizer,
        WorkflowAction,
        WorkflowResource,
        get_caller_identity,
    )

    authz = authorizer or NoopAuthorizer()

    async def _get_workflow_authz(workflow_id: str) -> WorkflowResource:
        """Load persisted authz context for a workflow.

        Queries the running Temporal workflow for its authorization context
        captured at trigger time. Fails closed when authorization is enabled
        and the context cannot be loaded.

        Parameters:
            workflow_id: The workflow run identifier.

        Returns:
            WorkflowResource populated with owner/namespace/name.

        Raises:
            HTTPException: 503 if authz is enabled and context lookup fails.
        """
        try:
            handle = temporal_client.get_workflow_handle(workflow_id)
            ctx = await handle.query(AgentWorkflow.get_authz_context)
            if ctx:
                return WorkflowResource(
                    workflow_id=workflow_id,
                    workflow_name=ctx.get("workflow_name"),
                    owner=ctx.get("owner_username"),
                    namespace=ctx.get("namespace"),
                )
        except Exception as exc:
            authz_mode = os.environ.get("WORKFLOW_AUTHZ", "none")
            if authz_mode != "none":
                raise HTTPException(
                    status_code=503,
                    detail=f"Cannot load authorization context for workflow '{workflow_id}': {exc}",
                ) from exc
            logger.warning("Failed to load authz context for '%s': %s", workflow_id, exc)
        return WorkflowResource(workflow_id=workflow_id)

    dependencies = [Depends(auth_dependency)] if auth_dependency else []
    router = APIRouter(
        prefix="/v1/workflows",
        tags=["workflows"],
        dependencies=dependencies,
    )

    @router.post("/run", status_code=status.HTTP_202_ACCEPTED)
    async def run_workflow(
        request: RunWorkflowRequest,
        caller=Depends(get_caller_identity),
    ) -> dict[str, str]:
        """Start a new workflow execution."""
        wf_name = request.workflow_name
        if not wf_name and request.definition:
            wf_name = request.definition.get("metadata", {}).get("name")
        decision = await authz.authorize(
            caller,
            WorkflowAction.TRIGGER,
            WorkflowResource(workflow_name=wf_name),
        )
        if not decision.allowed:
            raise HTTPException(status_code=403, detail=decision.reason)

        definition = request.definition

        if request.workflow_name and not definition:
            if not definition_store:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="workflow_name requires a definition store",
                )
            stored = await definition_store.get(request.workflow_name)
            if not stored:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Workflow '{request.workflow_name}' not found",
                )
            definition = stored.definition.model_dump()
            provider = request.provider or (
                ProviderConfig(**stored.definition.provider.model_dump())
                if stored.definition.provider
                else None
            )
        else:
            provider = request.provider

        if not definition:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Either definition or workflow_name is required",
            )
        if not provider:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Provider configuration is required",
            )

        from cloud_agents.workflow.temporal_validation import validate_definition

        validation_errors = validate_definition(definition, content_policy=content_policy)
        if validation_errors:
            if content_policy is not None:
                _emit_content_policy_audit(
                    workflow_id=request.workflow_id or "unknown",
                    definition=definition,
                    errors=validation_errors,
                )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"validation_errors": validation_errors},
            )

        if request.advisory is not None:
            advisory = request.advisory
        elif request.workflow_name and definition_store:
            stored_def = await definition_store.get(request.workflow_name)
            advisory = stored_def.definition.advisory if stored_def else False
        else:
            advisory = False

        workflow_id = request.workflow_id or f"wf-{uuid.uuid4().hex[:12]}"

        from cloud_agents.workflow.authorization import (
            WorkflowAuthzContext,
            parse_namespace_from_sa_username,
        )

        authz_ctx = WorkflowAuthzContext(
            owner_username=caller.username,
            owner_groups=caller.groups,
            workflow_name=definition.get("metadata", {}).get("name", ""),
            namespace=parse_namespace_from_sa_username(caller.username),
        )

        workflow_input = WorkflowInput(
            definition=definition,
            input_prompt=request.input_prompt,
            workflow_id=workflow_id,
            provider=provider,
            sandbox_image=request.sandbox_image,
            skills_image=request.skills_image,
            skills_paths=request.skills_paths,
            advisory=advisory,
            mcp_servers=request.mcp_servers,
            approval_policy=request.approval_policy,
            notifier_config=request.notifier_config,
            escalation_config=request.escalation_config,
            authz_context=authz_ctx,
        )

        try:
            await temporal_client.start_workflow(
                AgentWorkflow.run,
                workflow_input,
                id=workflow_id,
                task_queue=DEFAULT_TASK_QUEUE,
            )
        except Exception as exc:
            from temporalio.service import RPCError, RPCStatusCode

            if isinstance(exc, RPCError) and exc.status == RPCStatusCode.ALREADY_EXISTS:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Workflow '{workflow_id}' already exists",
                ) from exc
            logger.error("Workflow start failed for '%s': %s", workflow_id, type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal workflow error",
            ) from None

        emit_audit(
            event_type="workflow_started",
            workflow_id=workflow_id,
            details={
                "definition_name": definition.get("metadata", {}).get("name", ""),
                "provider": provider.name,
                "advisory": advisory,
            },
        )

        return {"workflow_id": workflow_id}

    if definition_store:

        @router.post("/definitions", status_code=status.HTTP_201_CREATED)
        async def submit_definition(
            body: dict[str, Any],
            caller=Depends(get_caller_identity),
        ) -> dict[str, Any]:
            """Submit a workflow definition to the store."""
            decision = await authz.authorize(
                caller, WorkflowAction.MANAGE_DEFS, WorkflowResource()
            )
            if not decision.allowed:
                raise HTTPException(status_code=403, detail=decision.reason)

            from cloud_agents.workflow.definition import WorkflowDefinition
            from cloud_agents.workflow.temporal_validation import validate_definition

            validation_errors = validate_definition(body, content_policy=content_policy)
            if validation_errors:
                if content_policy is not None:
                    _emit_content_policy_audit(
                        workflow_id="definition-submission",
                        definition=body,
                        errors=validation_errors,
                    )
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"validation_errors": validation_errors},
                )

            defn = WorkflowDefinition.model_validate(body)
            stored = await definition_store.save(defn)
            return {"name": stored.name, "version": stored.version}

        @router.get("/definitions")
        async def list_definitions(
            caller=Depends(get_caller_identity),
        ) -> list[dict[str, Any]]:
            """List all active workflow definitions."""
            decision = await authz.authorize(
                caller, WorkflowAction.VIEW_DEFS, WorkflowResource()
            )
            if not decision.allowed:
                raise HTTPException(status_code=403, detail=decision.reason)

            defs = await definition_store.list_all()
            return [{"name": d.name, "version": d.version} for d in defs]

        @router.get("/definitions/{name}")
        async def get_definition(
            name: str,
            caller=Depends(get_caller_identity),
        ) -> dict[str, Any]:
            """Get a workflow definition by name."""
            decision = await authz.authorize(
                caller, WorkflowAction.VIEW_DEFS, WorkflowResource()
            )
            if not decision.allowed:
                raise HTTPException(status_code=403, detail=decision.reason)

            stored = await definition_store.get(name)
            if not stored:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Definition '{name}' not found",
                )
            return {
                "name": stored.name,
                "version": stored.version,
                "definition": stored.definition.model_dump(),
            }

    @router.post("/{workflow_id}/approve")
    async def approve_workflow(
        workflow_id: str,
        request: ApproveRequest,
        caller=Depends(get_caller_identity),
    ) -> dict[str, str]:
        """Send an approval signal to a running workflow."""
        resource = await _get_workflow_authz(workflow_id)
        resource.step = request.step_name
        decision = await authz.authorize(
            caller,
            WorkflowAction.APPROVE,
            resource,
        )
        if not decision.allowed:
            raise HTTPException(status_code=403, detail=decision.reason)

        handle = temporal_client.get_workflow_handle(workflow_id)
        await handle.signal(
            AgentWorkflow.approve,
            args=[
                request.step_name,
                request.decision,
                request.selected_option_id,
                caller.username,
                caller.uid,
            ],
        )

        from datetime import UTC, datetime

        from cloud_agents.workflow.authorization import ApproverInfo

        approver = ApproverInfo(
            username=caller.username,
            uid=caller.uid,
            approved_at=datetime.now(tz=UTC).isoformat(),
        )

        event_type = "step_approved" if request.decision == "approved" else "step_denied"
        emit_audit(
            event_type=event_type,
            workflow_id=workflow_id,
            step_name=request.step_name,
            actor=caller.username,
            details={
                "decision": request.decision,
                "selected_option_id": request.selected_option_id,
                "approver": approver.model_dump(),
            },
        )
        return {"status": "signal_sent"}

    @router.get("/{workflow_id}")
    async def get_workflow_status(
        workflow_id: str,
        caller=Depends(get_caller_identity),
    ) -> dict[str, Any]:
        """Query the current workflow status."""
        resource = await _get_workflow_authz(workflow_id)
        view_decision = await authz.authorize(
            caller,
            WorkflowAction.VIEW,
            resource,
        )
        if not view_decision.allowed:
            raise HTTPException(status_code=403, detail=view_decision.reason)

        handle = temporal_client.get_workflow_handle(workflow_id)
        status_result = await handle.query(AgentWorkflow.get_status)
        if hasattr(status_result, "model_dump"):
            return status_result.model_dump()
        return {"steps": {}, "events": []}

    @router.get("/{workflow_id}/handoff")
    async def get_workflow_handoff(
        workflow_id: str,
        caller=Depends(get_caller_identity),
    ) -> dict[str, Any]:
        """Generate a CLI handoff context package for any workflow.

        Returns the workflow context serialized as markdown, a launch
        command for Claude Code, and the workflow ID. The context_markdown
        field is the primary interface for API consumers.
        """
        resource = await _get_workflow_authz(workflow_id)
        handoff_decision = await authz.authorize(
            caller,
            WorkflowAction.VIEW,
            resource,
        )
        if not handoff_decision.allowed:
            raise HTTPException(status_code=403, detail=handoff_decision.reason)

        handle = temporal_client.get_workflow_handle(workflow_id)
        try:
            status_result = await handle.query(AgentWorkflow.get_status)
            workflow_context = await handle.query(AgentWorkflow.get_workflow_context)
            step_transcripts = await handle.query(AgentWorkflow.get_step_transcripts)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow '{workflow_id}' not found or not queryable: {exc}",
            ) from exc

        from cloud_agents.workflow.escalation import (
            EscalationPackage,
            serialize_handoff_context,
        )

        # Build step snapshot from status query
        steps_snapshot: dict[str, Any] = {}
        if hasattr(status_result, "steps"):
            for k, v in status_result.steps.items():
                steps_snapshot[k] = (
                    v.model_dump() if hasattr(v, "model_dump") else v
                )

        events_list: list[dict[str, Any]] = []
        if hasattr(status_result, "events"):
            for e in status_result.events:
                events_list.append(
                    e.model_dump() if hasattr(e, "model_dump") else e
                )

        # Find the failed step
        failed_step = "unknown"
        for k, v in steps_snapshot.items():
            if isinstance(v, dict) and v.get("status") == "failed":
                failed_step = k
                break

        wf_name = "workflow"
        definition = None
        input_prompt = None
        provider_name = None
        if workflow_context:
            definition = workflow_context.get("definition")
            input_prompt = workflow_context.get("input_prompt")
            provider_name = workflow_context.get("provider_name")
            if definition:
                wf_name = definition.get("metadata", {}).get("name", "workflow")

        from datetime import UTC, datetime

        pkg = EscalationPackage(
            workflow_name=wf_name,
            step_name=failed_step,
            timestamp=datetime.now(tz=UTC).isoformat(),
            escalation={"type": "handoff_request"},
            workflow_snapshot=steps_snapshot,
            definition=definition,
            input_prompt=input_prompt,
            events=events_list or None,
            provider_name=provider_name,
            workflow_id=workflow_id,
            step_transcripts=step_transcripts or None,
        )

        context_md = serialize_handoff_context(pkg)
        launch_cmd = (
            f'claude -p "Continue this investigation. '
            f"The workflow '{wf_name}' (ID: {workflow_id}) needs attention. "
            f'Read the context above for details."'
        )

        return {
            "workflow_id": workflow_id,
            "context_markdown": context_md,
            "launch_command": launch_cmd,
        }

    @router.get("/{workflow_id}/steps/{step_key}/transcript")
    async def get_step_transcript(
        workflow_id: str,
        step_key: str,
        caller=Depends(get_caller_identity),
    ) -> dict[str, Any]:
        """Get the full transcript for a specific workflow step.

        Returns the agent's multi-turn execution transcript including
        tool calls, thinking, results, errors, and cost/token metrics.

        Parameters:
            workflow_id: Workflow execution identifier.
            step_key: Step output_key to retrieve transcript for.

        Returns:
            StepTranscript dict with events and metrics.

        Raises:
            HTTPException: 403 if unauthorized, 404 if step not found.
        """
        resource = await _get_workflow_authz(workflow_id)
        view_decision = await authz.authorize(
            caller,
            WorkflowAction.VIEW,
            resource,
        )
        if not view_decision.allowed:
            raise HTTPException(status_code=403, detail=view_decision.reason)

        handle = temporal_client.get_workflow_handle(workflow_id)
        try:
            transcripts = await handle.query(AgentWorkflow.get_step_transcripts)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow '{workflow_id}' not found or not queryable: {exc}",
            ) from exc

        if step_key not in transcripts:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Transcript for step '{step_key}' not found in workflow '{workflow_id}'",
            )

        return transcripts[step_key]

    @router.get("/{workflow_id}/events")
    async def get_workflow_events(
        workflow_id: str,
        caller=Depends(get_caller_identity),
    ) -> StreamingResponse:
        """Stream workflow events via SSE, polling status every second."""
        resource = await _get_workflow_authz(workflow_id)
        events_decision = await authz.authorize(
            caller,
            WorkflowAction.VIEW,
            resource,
        )
        if not events_decision.allowed:
            raise HTTPException(status_code=403, detail=events_decision.reason)

        handle = temporal_client.get_workflow_handle(workflow_id)
        seen_count = 0

        async def event_generator():
            nonlocal seen_count
            while True:
                try:
                    result = await handle.query(AgentWorkflow.get_status)
                    events = result.events if hasattr(result, "events") else []
                    for event in events[seen_count:]:
                        data = (
                            event.model_dump()
                            if hasattr(event, "model_dump")
                            else event
                        )
                        yield f"data: {json.dumps(data)}\n\n"
                    seen_count = len(events)

                    try:
                        desc = await handle.describe()
                        wf_status = desc.status
                        if wf_status in (
                            WorkflowExecutionStatus.COMPLETED,
                            WorkflowExecutionStatus.FAILED,
                            WorkflowExecutionStatus.CANCELED,
                            WorkflowExecutionStatus.TERMINATED,
                            WorkflowExecutionStatus.TIMED_OUT,
                        ):
                            yield 'data: {"type": "workflow.completed"}\n\n'
                            break
                    except Exception:
                        pass
                except Exception:
                    yield 'data: {"type": "workflow.error"}\n\n'
                    break

                await asyncio.sleep(1)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
        )

    @router.post("/{workflow_id}/cancel")
    async def cancel_workflow(
        workflow_id: str,
        caller=Depends(get_caller_identity),
    ) -> dict[str, str]:
        """Cancel a running workflow."""
        resource = await _get_workflow_authz(workflow_id)
        cancel_decision = await authz.authorize(
            caller,
            WorkflowAction.CANCEL,
            resource,
        )
        if not cancel_decision.allowed:
            raise HTTPException(status_code=403, detail=cancel_decision.reason)

        handle = temporal_client.get_workflow_handle(workflow_id)
        await handle.cancel()
        return {"status": "cancelled"}

    # -- CLI session endpoints -------------------------------------------------
    # Session endpoints live at /v1/cli-sessions (separate prefix from
    # /v1/workflows), so they use a sibling router included via a
    # shared parent.
    if cli_session_launcher is not None:
        session_router = APIRouter(
            prefix="/v1/cli-sessions",
            tags=["cli-sessions"],
            dependencies=dependencies,
        )

        @session_router.get("")
        async def list_cli_sessions(
            caller=Depends(get_caller_identity),
        ) -> list[dict[str, Any]]:
            """List all active CLI sessions."""
            decision = await authz.authorize(
                caller, WorkflowAction.VIEW, WorkflowResource()
            )
            if not decision.allowed:
                raise HTTPException(status_code=403, detail=decision.reason)

            sessions = cli_session_launcher.list_sessions()
            return [s.model_dump() for s in sessions]

        @session_router.get("/{session_id}")
        async def get_cli_session(
            session_id: str,
            caller=Depends(get_caller_identity),
        ) -> dict[str, Any]:
            """Get status of a specific CLI session."""
            decision = await authz.authorize(
                caller, WorkflowAction.VIEW, WorkflowResource()
            )
            if not decision.allowed:
                raise HTTPException(status_code=403, detail=decision.reason)

            info = cli_session_launcher.get_status(session_id)
            if info is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"CLI session '{session_id}' not found",
                )
            return info.model_dump()

        @session_router.delete("/{session_id}")
        async def delete_cli_session(
            session_id: str,
            caller=Depends(get_caller_identity),
        ) -> dict[str, str]:
            """Terminate a running CLI session."""
            decision = await authz.authorize(
                caller, WorkflowAction.CANCEL, WorkflowResource()
            )
            if not decision.allowed:
                raise HTTPException(status_code=403, detail=decision.reason)

            if cli_session_launcher.get_status(session_id) is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"CLI session '{session_id}' not found",
                )

            await cli_session_launcher.terminate(session_id, cli_session_spawner)
            return {"status": "terminated"}

        @session_router.post("/{session_id}/messages")
        async def send_cli_session_message(
            session_id: str,
            request: SendMessageRequest,
            caller=Depends(get_caller_identity),
        ) -> dict[str, str]:
            """Send a message to a running CLI session."""
            decision = await authz.authorize(
                caller, WorkflowAction.SESSION_MESSAGE, WorkflowResource()
            )
            if not decision.allowed:
                raise HTTPException(status_code=403, detail=decision.reason)

            if cli_session_launcher.get_status(session_id) is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"CLI session '{session_id}' not found",
                )

            await cli_session_launcher.send_message(
                session_id, cli_session_spawner, request.message
            )
            return {"status": "sent"}

        @session_router.get("/{session_id}/output")
        async def get_cli_session_output(
            session_id: str,
            caller=Depends(get_caller_identity),
        ) -> StreamingResponse:
            """Stream CLI session output via SSE."""
            decision = await authz.authorize(
                caller, WorkflowAction.VIEW, WorkflowResource()
            )
            if not decision.allowed:
                raise HTTPException(status_code=403, detail=decision.reason)

            if cli_session_launcher.get_status(session_id) is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"CLI session '{session_id}' not found",
                )

            async def output_generator():
                try:
                    async for event in cli_session_launcher.monitor_output(
                        session_id, cli_session_spawner
                    ):
                        data = event.model_dump()
                        yield f"data: {json.dumps(data)}\n\n"
                except Exception:
                    yield 'data: {"event_type": "error", "data": "stream ended"}\n\n'
                yield 'data: {"event_type": "done", "data": ""}\n\n'

            return StreamingResponse(
                output_generator(),
                media_type="text/event-stream",
            )

        # Wrap both routers in a parent so they share auth dependencies
        # but have independent prefixes.
        parent = APIRouter()
        parent.include_router(router)
        parent.include_router(session_router)
        return parent

    return router
