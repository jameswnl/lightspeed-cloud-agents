# Review: `implementation-plan.md` and `productization-roadmap.md`

## Findings

### 1. Major (Quality): the new MCP file-reference contract introduces work that is not actually scheduled or fully verified
The latest draft fixes the prior contradiction correctly: the workflow runner now keeps secret values out of `LIGHTSPEED_MCP_SERVERS` and passes file references that the sandbox resolves locally. That is a better contract.

However, the plan now explicitly says:

- the sandbox must support `{"file": "/path"}` header references in MCP config
- if it does not already support that shape, an upstream or local sandbox extension is required

That new dependency is not represented as its own task anywhere in the plan. The only listed upstream sandbox tasks are still the older `executionResult` and HTTP 502 PRs. The verification plan also stops at "mock sandbox verifies it receives the MCP config," which checks transport but not the riskiest behavior: actually resolving a mounted secret-backed file reference during an MCP connection.

**Why it matters:** this is now the critical path for the new MCP design. If sandbox-side file-reference support does not already exist, T2 is not self-contained and the current task breakdown understates both implementation scope and the hardest verification step.

**Recommended fix:** make the file-reference dependency explicit in the task graph:
- either add a concrete T2 subtask for sandbox support of MCP header file references
- or add a separate upstream/local sandbox task alongside T8

Then extend verification to cover the real edge:
- sandbox receives `{"file": "/var/secrets/mcp/..."}`
- sandbox reads the mounted file
- outbound MCP request uses the resolved header value

## Perspective Check
- Functionality: covered; no new runtime contradiction remains in the doc itself, but the sandbox-side file-ref support is still an unscheduled dependency.
- Quality: covered; remaining gap is that the task breakdown and verification plan do not yet fully account for the new MCP contract.
- Security: covered; the file-ref design is stronger than env-var inlining, and no new major security gaps stood out in this pass.

## Open Questions / Assumptions

- Does `lightspeed-agentic-sandbox` already support MCP header values expressed as file references?
- If not, should that support land upstream, or can Cloud Agents carry it locally for productionization?

## Summary

The plan set is very close, and the previous remaining issues are resolved. It is still not `LGTM` because the improved MCP secret model now depends on sandbox-side file-reference support that is acknowledged in prose but not yet scheduled or tested as a first-class task.
