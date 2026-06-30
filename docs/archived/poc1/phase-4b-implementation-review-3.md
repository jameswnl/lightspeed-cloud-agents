# Review: Phase 4b Implementation (Commit `dec6aaf3`)

## Findings

### 1. Major: PostgreSQL persistence is still not wired into workflow-runner startup

The key remaining issue from the prior Phase 4b reviews is still present:

- `PostgresPersistence` exists
- workflow runner entrypoint still constructs `WorkflowExecutor(defn, registry)` directly
- there is still no startup selection logic for choosing database-backed persistence

#### Why this matters

This remains a real **functionality** and **quality** gap:

- the branch contains a persistence backend
- the runner still cannot actually use it in deployed startup
- the production-persistence milestone is still only partially realized

Until the runner can select `PostgresPersistence`, the feature is still library-only.

#### Recommendation

The next Phase 4b implementation step should wire persistence selection into `workflow.entrypoint`, most likely through env/config choosing between in-memory, file-based, and PostgreSQL persistence backends.

### 2. Major: on-demand spawning is implemented as standalone infrastructure, but still not connected to workflow execution

This commit adds:

- `AgentSpawner` ABC
- `KubernetesSpawner`
- `PodmanSpawner`

and the commit message explicitly acknowledges the production gap:

- spawner is not yet wired into `WorkflowExecutor`
- workflow steps still do not support `spawn: on-demand`

#### Why this matters

This means Phase 4b now has two major capability tracks implemented as components but not yet reachable from the live workflow runtime:

- PostgreSQL persistence
- on-demand spawning

That is useful progress, but it is not yet a complete user-visible feature.

#### Recommendation

Keep treating spawner work as foundational until:

- workflow definition supports `spawn: on-demand`
- executor chooses spawn vs pre-deployed path
- end-to-end tests cover the full lifecycle

## What Improved

This commit is still solid incremental progress:

- spawner abstractions now exist
- Kubernetes and Podman backends are implemented
- targeted spawner tests pass
- the commit is explicit about the remaining integration gap instead of overclaiming completion

## Perspective Check

- Functionality: new spawner capability is implemented as building blocks, but not yet integrated into workflow execution
- Quality: commit is clear about what is and is not finished; tests cover the new module in isolation
- Security: Podman spawner continues to document the host-control trust boundary clearly; no new regression observed in this slice

## Verification

I ran:

```bash
uv run pytest tests/unit/agents/workflow/test_entrypoint.py tests/unit/agents/workflow/test_postgres_persistence.py -q
```

Result:

- **12 passed**

## Summary

This is useful Phase 4b foundation work, but the overall approval state does not change:

- PostgreSQL persistence is still not wired into the workflow runner
- on-demand spawning is still not wired into the workflow executor

So Phase 4b implementation is still **not** at `LGTM` yet.
