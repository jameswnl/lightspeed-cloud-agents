# Tool Registry & Spawn Mode Architecture

## Spawn Mode Dispatch

```mermaid
flowchart TD
    YAML["Workflow Definition (YAML)<br/>steps with spawn: none / local / ephemeral"]
    YAML --> dispatch["get_step_executor()<br/>dispatch.py"]

    dispatch -->|"spawn: none"| direct["DirectExecutor<br/>Single LLM call<br/>No tools, no agent loop"]
    dispatch -->|"spawn: local"| subprocess["SubprocessExecutor<br/>pydantic-ai Agent<br/>+ registered tools<br/>in subprocess"]
    dispatch -->|"spawn: ephemeral"| sandbox["SandboxExecutor<br/>OpenShell container<br/>+ MCP tools"]

    direct --> result["StepResult<br/>.status .output .transcript<br/>.input_tokens .output_tokens"]
    subprocess --> result
    sandbox --> result
```

## Tool Systems by Spawn Mode

| | spawn: none | spawn: local | spawn: ephemeral |
|---|---|---|---|
| **Tool support** | None | pydantic-ai `@tool` functions | MCP + Shell + Filesystem + Skills |
| **Tool source** | N/A | ToolRegistry (in-process) | Sandbox image + MCP servers |
| **Tool isolation** | N/A | Process boundary (subprocess) | Container boundary (SecurityContext, NetworkPolicy) |
| **Agent loop** | No (single call) | Yes (`Agent.run`) | Yes (agent SDK in container) |
| **LLM transport** | pydantic-ai `model_request` | pydantic-ai `Agent.run` | Agent SDK in sandbox |
| **Providers** | All (via pydantic-ai) | All (via pydantic-ai) | Configured in sandbox env vars |
| **Infrastructure needed** | Nothing (just PostgreSQL) | Nothing (just PostgreSQL) | OpenShell gateway + sandbox image |

## Tool Registry Architecture (spawn: local)

```mermaid
flowchart TD
    subgraph startup["Application Startup"]
        decorator["@step_tool('kubectl_get')<br/>def kubectl_get(resource, ns): ..."]
        programmatic["register_tool('http_request', http_fn)"]
    end

    subgraph registry["ToolRegistry — tools.py"]
        store["_REGISTRY: dict[str, pydantic_ai.Tool]"]
        entries["kubectl_get → Tool(fn)<br/>read_logs → Tool(fn)<br/>http_request → Tool(fn)<br/>read_file → Tool(fn)"]
        api["register_tool(name, func)<br/>get_tools(names) → list of Tool<br/>list_tools() → list of str"]
    end

    decorator --> store
    programmatic --> store
    store --- entries
    store --- api

    step_tools["step.tools: ['kubectl_get', 'read_logs']"]
    api -->|"get_tools()"| resolved["Returns: [Tool(kubectl_get), Tool(read_logs)]<br/>Raises ValueError for unknown names"]

    step_tools --> resolved
    resolved --> executor["SubprocessExecutor.run()<br/>Serializes tool names via stdin JSON"]

    subgraph child["Child Process — subprocess_child.py"]
        direction TB
        load["tools = get_tools(input['tools'])"]
        agent["agent = Agent(<br/>    model_string,<br/>    instructions=system_prompt,<br/>    tools=tools,<br/>)"]
        run["result = agent.run(prompt)"]
        loop["Agent loop:<br/>LLM → tool_call → execute → LLM<br/>→ ... → final answer"]

        load --> agent --> run --> loop
    end

    executor -->|"subprocess<br/>boundary"| child
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

    subgraph direct["DirectExecutor"]
        d_provider["provider.py:<br/>to_model_string()<br/>ensure_credentials_env()"]
        d_llm["pydantic-ai:<br/>model_request()"]
        d_tools["Tools: NONE"]
        d_provider --> d_llm
    end

    subgraph subprocess["SubprocessExecutor"]
        s_fork["Fork subprocess:<br/>python -m subprocess_child"]
        subgraph child_proc["Child Process"]
            s_provider["provider.py:<br/>to_model_string()"]
            s_tools["tools.py:<br/>get_tools()"]
            s_agent["pydantic-ai:<br/>Agent.run()"]
            s_provider --> s_agent
            s_tools --> s_agent
        end
        s_fork --> child_proc
    end

    subgraph sandbox["SandboxExecutor"]
        sb_spawn["step_runner.py:<br/>spawn container"]
        sb_run["POST /v1/agent/run"]
        sb_events["GET /v1/agent/events"]
        sb_destroy["destroy container"]
        sb_spawn --> sb_run --> sb_events --> sb_destroy
    end

    direct --> result["StepResult<br/>.status  .output  .transcript<br/>.input_tokens  .output_tokens  .duration_ms"]
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
        map["_PROVIDER_NAME_MAP:<br/>openai → openai<br/>anthropic → anthropic<br/>gemini → google-gla<br/>azure → openai<br/>bedrock → bedrock"]
    end

    subgraph output["pydantic-ai Model String"]
        openai_out["openai:gpt-4o"]
        anthropic_out["anthropic:claude-sonnet-5"]
        gemini_out["google-gla:gemini-2.5-pro"]
        azure_out["openai:gpt-4o<br/>with Azure endpoint"]
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
        modes2["spawn: none → triage, classify, summarize<br/>spawn: local → K8s queries, log reading"]
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
