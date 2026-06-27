# Proposal: Shared Agent Sandbox Runtime for Lightspeed Platform

**Date**: 2026-06-26
**Author**: James Wong
**Audience**: OpenShift Lightspeed Agentic team, Lightspeed-stack/core team

## Summary

The `lightspeed-agentic-sandbox` container, currently maintained by the OpenShift Lightspeed team as the agent runtime for the agentic operator, should become the **shared standard agent runtime** for the broader Lightspeed platform. Lightspeed-stack/core adopts it as the default execution container for all agent workflows, replacing the need for a separate generic runtime image.

This creates a clean ownership split: the Lightspeed Agentic team owns the **agent execution environment** (sandbox), Lightspeed-stack/core owns the **workflow orchestration layer** (Temporal-based), and product teams bring their own **agent definitions, tools, and workflows**.

## Problem

Today there are two parallel agent runtime implementations:

| | lightspeed-agentic-sandbox | lightspeed-stack agent-runtime |
|---|---|---|
| **Owner** | OpenShift Lightspeed Agentic team | Lightspeed-stack/core team |
| **LLM SDKs** | Claude Code, OpenAI agents, Gemini ADK | Pydantic AI |
| **Cluster tools** | kubectl, oc, git, ripgrep, jq | None |
| **Base image** | RHEL 9, Konflux-built, security-hardened | Standard Python, dev-only |
| **Security** | Non-root, read-only FS, drop ALL capabilities | Basic |
| **Tools** | SDK-native (Bash, Read, Glob, Grep, Skill) | Python functions from modules |
| **Container size** | ~330 MB | ~150 MB |
| **Production readiness** | Yes (Konflux pipeline, RHEL base, CVE scanning) | No (PoC) |

Maintaining two runtime images means duplicated effort on:
- Security hardening and CVE patching
- Base image updates and Konflux integration
- Cluster tool packaging (kubectl, oc)
- Skills loading and OCI image volume support
- MCP server integration (both have it planned, neither has it done)

## Proposal

### 1. Adopt the sandbox as the shared default runtime

Lightspeed-stack/core uses `lightspeed-agentic-sandbox` as the container image for all ephemeral agent pods. The `POST /v1/agent/run` HTTP contract becomes the standard interface between the orchestrator and agent pods.

The sandbox already supports what product teams need:
- **Multiple LLM providers** via `LIGHTSPEED_AGENT_PROVIDER` (Claude, OpenAI, Gemini)
- **Structured output** via `outputSchema` (JSON Schema enforcement by the SDK)
- **Cluster operations** via built-in kubectl/oc
- **Skills** via OCI image volumes mounted at `/app/skills`
- **Configurable model** via provider-specific env vars (or the upcoming `LIGHTSPEED_MODEL` generic var)

### 2. Define the shared contract

The sandbox's HTTP contract is already clean and stable:

**Request** — `POST /v1/agent/run`
```json
{
  "query": "Diagnose why pods are crash-looping in namespace X",
  "systemPrompt": "You are a cluster diagnostic agent...",
  "outputSchema": {
    "type": "object",
    "properties": {
      "summary": { "type": "string" },
      "risk_level": { "type": "string", "enum": ["low", "medium", "high"] },
      "actions": { "type": "array", "items": { "type": "object" } }
    },
    "required": ["summary", "risk_level"]
  },
  "context": {
    "targetNamespaces": ["production"],
    "approvedOption": { "..." }
  },
  "timeout_ms": 300000
}
```

**Response**
```json
{
  "success": true,
  "summary": "Root cause identified: OOMKilled due to memory limit of 256Mi",
  "risk_level": "medium",
  "actions": [
    { "type": "patch", "description": "Increase memory limit to 512Mi" }
  ]
}
```

The orchestrator (operator or Temporal) doesn't need to know which SDK runs inside the container. It sends a prompt with a schema, gets structured JSON back.

### 3. Prioritize MCP server support in the sandbox

MCP servers are the key to making the sandbox extensible without changing the container image. Today, custom tools require either:
- Modifying the sandbox code (not scalable)
- Mounting skills as OCI image volumes (works, but limited to file-based tools)

With MCP support, product teams can run their own tool servers (ServiceNow, PagerDuty, Jira, internal APIs) and the sandbox agent calls them via MCP protocol. The agent gets new capabilities without any container image change.

The sandbox already has `LIGHTSPEED_MCP_SERVERS` env var wiring in the operator's template derivation code. The sandbox's Gemini provider has a TODO for MCP. This should be prioritized as a cross-team deliverable.

### 4. Skills via OCI image volumes as the standard tool distribution

The operator already packages skills as OCI container images (`agentic-skills` repo) and mounts them into sandbox pods as Kubernetes image volumes. This mechanism:
- Works on Kubernetes natively
- Works on Podman (Podman supports OCI image volumes)
- Enables versioned, immutable tool distribution
- Doesn't require rebuilding the sandbox image

Lightspeed-stack/core's spawner should adopt this pattern: mount skills OCI images as volumes on ephemeral pods instead of ConfigMap-based tool distribution.

### 5. Keep Pydantic AI runtime as an optional lightweight alternative

The sandbox is the right default for cluster operations. But some use cases don't need kubectl/oc, don't need a 330 MB container, and benefit from Python function tools with Pydantic model validation. The lightweight Pydantic AI runtime remains available for these cases, selectable per workflow step.

## Ownership Model

```
┌─────────────────────────────────────────────────────────────────┐
│                    LIGHTSPEED PLATFORM                           │
│                                                                 │
│  ┌───────────────────────────┐  ┌────────────────────────────┐  │
│  │  Lightspeed Agentic Team  │  │  Lightspeed-stack/core     │  │
│  │                           │  │                            │  │
│  │  Owns:                    │  │  Owns:                     │  │
│  │  • Sandbox container      │  │  • Temporal orchestrator   │  │
│  │  • SDK integrations       │  │  • Spawner (K8s + Podman)  │  │
│  │  • Cluster tools          │  │  • Workflow definitions    │  │
│  │  • Security hardening     │  │  • Approval gates          │  │
│  │  • Skills packaging       │  │  • SSE / observability     │  │
│  │  • MCP server support     │  │  • Agent registry          │  │
│  │                           │  │  • Dual deployment         │  │
│  │  Contract:                │  │                            │  │
│  │  POST /v1/agent/run       │  │  Contract:                 │  │
│  │  (prompt + schema → JSON) │  │  Workflow YAML + agent     │  │
│  │                           │  │  definitions               │  │
│  └───────────┬───────────────┘  └─────────────┬──────────────┘  │
│              │                                │                  │
│              │  shared runtime image          │  orchestration   │
│              ▼                                ▼                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Product Teams (Ansible, RHDH, ACS, ...)                 │   │
│  │                                                          │   │
│  │  Bring:                                                  │   │
│  │  • Workflow YAML (steps, conditions, approval policy)    │   │
│  │  • Agent prompts + output schemas                        │   │
│  │  • Skills (OCI image volumes)                            │   │
│  │  • MCP tool servers (optional)                           │   │
│  │  • Domain knowledge packages                             │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

## What Each Team Gets

### OpenShift Lightspeed Agentic team
- **Broader adoption** of the sandbox they already maintain — more users, more feedback, more justification for investment
- **Cleaner scope** — own the execution environment, not the orchestration. The operator remains available for OCP-native CRD workflows
- **Shared MCP effort** — MCP server support benefits both the operator and Temporal workflows

### Lightspeed-stack/core team
- **Production-ready runtime** without building one — RHEL base, Konflux pipeline, security hardening, kubectl/oc, multi-SDK support
- **Focus on orchestration** — Temporal workflows, spawner, approval, observability. No need to maintain a container image
- **Skills ecosystem** — access to `agentic-skills` repo and the OCI packaging infrastructure

### Product teams
- **One runtime to learn** — the sandbox contract is simple: send prompt + schema, get JSON back
- **Choice of LLM provider** — Claude Code, OpenAI, Gemini via env var
- **Cluster tools included** — kubectl, oc, git available in every agent pod
- **Extensible via MCP** — add custom tools without changing the runtime image

## Required Changes

### In lightspeed-agentic-sandbox

| Change | Priority | Effort |
|---|---|---|
| MCP server support (read `LIGHTSPEED_MCP_SERVERS`, create MCPToolset) | High | 1-2 weeks |
| Health endpoint alignment (`/healthz` in addition to `/health`) | Low | 1 day |
| Documentation: contract spec for external consumers | Medium | 1 week |

### In lightspeed-stack/core

| Change | Priority | Effort |
|---|---|---|
| `RemoteAgentClient` adapter for sandbox contract (`/v1/agent/run`) | High | 2-3 days |
| Spawner: OCI image volume mounting for skills | Medium | 1 week |
| Workflow step `runtime` field (sandbox vs generic) | Medium | 2-3 days |
| Documentation: how to define agents for the sandbox runtime | Medium | 1 week |

### Shared

| Change | Priority | Effort |
|---|---|---|
| Agree on `outputSchema` conventions for common use cases | High | Design discussion |
| Skills packaging guide (OCI image volumes) | Medium | 1 week |
| Integration test: Temporal workflow → sandbox pod → real LLM | Medium | 1 week |

## Deployment Scenarios

### OCP with operator (current Lightspeed workflow)

```
Proposal CRD → Operator → SandboxClaim → Sandbox pod → LLM
```
Unchanged. The operator continues to work for teams that want K8s-native CRD workflows.

### OCP with Temporal (new general-purpose workflow)

```
Workflow YAML → FastAPI → Temporal → Worker → K8s Job (sandbox image) → LLM
```
Same sandbox image, different orchestrator. Product teams get flexible workflows with approval gates, conditions, and parallel steps.

### Podman with Temporal

```
Workflow YAML → FastAPI → Temporal → Worker → Podman container (sandbox image) → LLM
```
Same sandbox image, Podman spawner instead of K8s. Behavioral parity with OCP deployment.

## Non-Goals

- **Replacing the operator for OpenShift Lightspeed's remediation use case** — the operator continues to serve that specific workflow. This proposal is about enabling other teams.
- **Merging the sandbox and lightspeed-stack repos** — they remain separate repos with separate owners. The contract is the integration point.
- **Requiring all product teams to use the sandbox** — the lightweight Pydantic AI runtime remains available. The sandbox is the default, not the only option.

## Next Steps

1. **Share this proposal** with the Lightspeed Agentic team for feedback
2. **Agree on the contract** — confirm `POST /v1/agent/run` is stable and documented
3. **Prototype integration** — Temporal activity calling a sandbox pod on Kind with a real LLM (we've already done this with GPT-5.5 during operator testing)
4. **Prioritize MCP in the sandbox** — this is the highest-value shared deliverable
5. **Publish skills packaging guide** — how product teams create OCI skill images
