# Tool Registry & Spawn Mode Architecture

> **Note:** Diagrams marked *(Planned)* describe the target architecture from issue #131.
> Items marked *(planned)* in the comparison table are not yet implemented.

## Spawn Mode Dispatch

```mermaid
flowchart TD
    YAML["Workflow Definition (YAML)<br/>steps with spawn: none / local / ephemeral"]
    YAML --> dispatch["get_step_executor()<br/>dispatch.py"]

    dispatch -->|"spawn: none"| direct["DirectExecutor<br/>pydantic-ai in-process<br/>No isolation"]
    dispatch -->|"spawn: local"| subprocess["SubprocessExecutor<br/>pydantic-ai in subprocess<br/>Process isolation"]
    dispatch -->|"spawn: ephemeral"| sandbox["SandboxExecutor<br/>OpenShell container<br/>+ MCP tools"]

    direct --> result["StepResult<br/>.status .output .error"]
    subprocess --> result
    sandbox --> result
```

## Tool Systems by Spawn Mode

| | spawn: none | spawn: local | spawn: ephemeral |
|---|---|---|---|
| **Isolation** | None (in-process) | Process boundary (subprocess) | Container boundary (SecurityContext, NetworkPolicy) |
| **Tool support** | `@step_tool` registered functions | `@step_tool` registered functions | MCP + Shell + Filesystem + Skills |
| **MCP servers** | Remote (HTTP/SSE) | Remote (HTTP/SSE) | Local + remote (inside container) |
| **Skills** | `CLOUD_AGENTS_SKILLS_PATHS` directories with `SKILL.md` | `CLOUD_AGENTS_SKILLS_PATHS` directories with `SKILL.md` | OCI image via init container |
| **Tool source** | ToolRegistry + MCP + SkillsCapability | ToolRegistry + MCP + SkillsCapability | Sandbox image + MCP servers |
| **Agent loop** | `Agent.run` if tools, MCP, or skills; `model_request` otherwise | `Agent.run` if tools, MCP, or skills; `model_request` otherwise | Yes (agent SDK in container) |
| **LLM transport** | pydantic-ai `model_request` or `Agent.run` | pydantic-ai `model_request` or `Agent.run` (in subprocess) | Agent SDK in sandbox |
| **Timeout enforcement** | pydantic-ai `model_settings.timeout` | Hard kill (`proc.kill()`) | Container terminate |
| **Providers** | All (via pydantic-ai) | All (via pydantic-ai) | Configured in sandbox env vars |
| **Infrastructure needed** | Nothing (just PostgreSQL) | Nothing (just PostgreSQL) | OpenShell gateway + sandbox image |
| **Best for** | Trusted tools, low latency | Semi-trusted tools, crash safety | Untrusted code, shell access |

## Tool Registry Architecture (spawn: none + local)

```mermaid
flowchart TD
    subgraph sources["Three Tool Sources"]
        decorators["@step_tool decorated<br/>functions via ToolRegistry"]
        skills["CLOUD_AGENTS_SKILLS_PATHS<br/>directories with SKILL.md<br/>→ SkillsCapability"]
        mcp["MCP servers<br/>(HTTP/SSE remote)<br/>→ MCPToolset"]
    end

    subgraph agent_construction["Agent Construction"]
        tools_param["tools=get_tools(step.tools)"]
        toolsets_param["toolsets=[MCPToolset(...)]"]
        caps_param["capabilities=[SkillsCapability(...)]"]
    end

    decorators --> tools_param
    mcp --> toolsets_param
    skills --> caps_param

    tools_param --> agent["Agent(<br/>model,<br/>tools=...,<br/>toolsets=...,<br/>capabilities=...<br/>)"]
    toolsets_param --> agent
    caps_param --> agent

    agent --> direct_exec["spawn: none<br/>Agent runs in-process"]
    agent --> subprocess_exec["spawn: local<br/>Agent runs in subprocess"]
```

## Data Flow: Step Execution Across Spawn Modes

```mermaid
flowchart TD
    yaml["Workflow YAML"] --> translator["graph_translator<br/>build_graph()"]

    translator --> stepinput["Build StepInput<br/>.prompt  .provider  .tools<br/>.output_schema  .context<br/>.system_prompt  .timeout_seconds"]

    stepinput --> dispatch["get_step_executor(step)"]

    dispatch -->|"spawn: none"| direct
    dispatch -->|"spawn: local"| subprocess
    dispatch -->|"spawn: ephemeral"| sandbox

    subgraph direct["DirectExecutor (no isolation)"]
        d_provider["provider.py:<br/>to_model_string()<br/>ensure_credentials_env()"]
        d_llm["pydantic-ai:<br/>model_request()"]
        d_provider --> d_llm
    end

    subgraph subprocess["SubprocessExecutor (process isolation)"]
        s_fork["Fork subprocess:<br/>python -m subprocess_child"]
        subgraph child_proc["Child Process"]
            s_provider["provider.py:<br/>to_model_string()"]
            s_llm["pydantic-ai:<br/>model_request()"]
            s_provider --> s_llm
        end
        s_fork --> child_proc
    end

    subgraph sandbox["SandboxExecutor (container isolation)"]
        sb_spawn["step_runner.py:<br/>spawn container"]
        sb_run["POST /v1/agent/run"]
        sb_events["GET /v1/agent/events"]
        sb_destroy["destroy container"]
        sb_spawn --> sb_run --> sb_events --> sb_destroy
    end

    direct --> result["StepResult<br/>.status  .output  .error"]
    subprocess --> result
    sandbox --> result

    result --> state["Store in workflow state<br/>under step.output_key<br/>Available as context<br/>to subsequent steps"]
```

## Provider Mapping (provider.py — shared by all modes)

```mermaid
flowchart LR
    subgraph input["Workflow Provider Config"]
        openai_in["name: openai<br/>model: gpt-4o"]
        anthropic_in["name: anthropic<br/>model: claude-sonnet-5"]
        gemini_in["name: gemini<br/>model: gemini-2.5-pro"]
        azure_in["name: azure<br/>model: gpt-4o<br/>base_url: https://..."]
        bedrock_in["name: bedrock<br/>model: us.anthropic.claude-..."]
    end

    subgraph mapper["to_model_string()"]
        map["_PROVIDER_NAME_MAP:<br/>openai → openai<br/>anthropic → anthropic<br/>gemini → google-gla<br/>azure → azure<br/>bedrock → bedrock"]
    end

    subgraph output["pydantic-ai Model String"]
        openai_out["openai:gpt-4o"]
        anthropic_out["anthropic:claude-sonnet-5"]
        gemini_out["google-gla:gemini-2.5-pro"]
        azure_out["azure:gpt-4o"]
        bedrock_out["bedrock:us.anthropic.claude-..."]
    end

    openai_in --> mapper --> openai_out
    anthropic_in --> mapper --> anthropic_out
    gemini_in --> mapper --> gemini_out
    azure_in --> mapper --> azure_out
    bedrock_in --> mapper --> bedrock_out
```

## Credential Resolution

```mermaid
flowchart TD
    cred["credentials_secret: 'my-api-key'"]
    cred --> normalize["Normalize: MY_API_KEY"]
    normalize --> env_check{"os.environ.get('MY_API_KEY')"}

    env_check -->|"Found"| set_target["Set provider env var<br/>e.g. OPENAI_API_KEY"]
    env_check -->|"Not found"| fallback["Fall back to provider default"]

    fallback --> defaults["openai → OPENAI_API_KEY<br/>anthropic → ANTHROPIC_API_KEY<br/>gemini → GOOGLE_API_KEY<br/>azure → AZURE_OPENAI_API_KEY"]

    defaults --> set_target
    set_target --> ready["pydantic-ai reads credential<br/>from well-known env var"]
```

## Growth Path (Issue #125 — Embedded Mode)

```mermaid
flowchart TD
    subgraph stage1["Stage 1: lightspeed-stack only"]
        lcs1["lightspeed-stack + PostgreSQL"]
        cap1["/query, /a2a, /responses<br/>Conversational Q&A"]
    end

    stage1 -->|"cloud_agents:<br/>enabled: true"| stage2

    subgraph stage2["Stage 2: + cloud agents — spawn: none + local"]
        lcs2["lightspeed-stack + cloud agents + PostgreSQL"]
        cap2["/v1/workflows/*<br/>Multi-step workflows, approval gates<br/>Structured LLM calls, pydantic-ai tools<br/>NO new infrastructure"]
        modes2["spawn: none → triage, classify, K8s queries (trusted tools)<br/>spawn: local → same tools with crash isolation"]
    end

    stage2 -->|"Deploy OpenShell<br/>gateway"| stage3

    subgraph stage3["Stage 3: + sandbox — spawn: ephemeral"]
        lcs3["lightspeed-stack + cloud agents + PostgreSQL"]
        os3["OpenShell gateway + sandbox pods"]
        cap3["+ Full container isolation<br/>+ Shell commands, MCP servers<br/>+ File mutation, untrusted code"]
        lcs3 --> os3
    end

    stage3 -->|"Optional:<br/>Add Temporal"| stage4

    subgraph stage4["Stage 4: + crash recovery"]
        lcs4["lightspeed-stack + cloud agents + PostgreSQL"]
        infra4["OpenShell gateway + Temporal server"]
        cap4["+ Durable workflow execution<br/>+ Crash recovery mid-step<br/>+ Scheduled workflows"]
        lcs4 --> infra4
    end

    style stage1 fill:#f5f5f5
    style stage2 fill:#e8f5e9
    style stage3 fill:#e3f2fd
    style stage4 fill:#fff3e0
```
