# Review: Phase 6 follow-up commit (`6ae8a380`)

## Findings

### 1. High: shared definition versioning still breaks across replicas or restarts
This commit fixes shared reads, listing, and deletion by consulting shared persistence, but `DefinitionStore.save()` still assigns the next version using only the process-local `_versions` cache:

- `versions = self._versions.setdefault(name, [])`
- `version = len(versions) + 1`

That means if replica A saves `workflow-x` version 1, then replica B or a restarted process saves `workflow-x` again, it will compute version 1 again unless its local cache was already warmed. The shared store read path is no longer the source of truth for version allocation.

Why it matters:
- Phase 6 explicitly requires definition versioning for immutable run snapshots
- a second replica can generate duplicate version numbers for the same definition name
- once version numbers drift, `run by name` no longer has a reliable version history to bind to

Recommended fix:
- allocate the next definition version from shared persistence, not from local `_versions`
- add a cross-instance test that saves the same definition name from two fresh store instances and asserts versions increment monotonically

### 2. Medium: `get_version()` still uses only the process-local cache
Even after this commit, `get_version()` reads only `_versions` and never consults shared persistence. So a fresh replica can read the latest definition via `get()`, but still cannot retrieve historical versions through the versioned API behavior promised in Phase 6.

Why it matters:
- the read path is only partially shared
- versioned lookup behavior still disagrees across replicas

Recommended fix:
- back `get_version()` with shared persistence as well, or rebuild the version history from shared state on demand

## Perspective Check
- Functionality: remaining gaps. Shared definition reads are improved, but shared version history is still not correct across replicas.
- Quality: remaining gaps. The new tests cover cross-instance visibility, but not cross-instance version allocation or versioned lookup.
- Security: no new security findings in this commit.

## Verification
- Reviewed commit `6ae8a380` and the updated `src/agents/workflow/definition_store.py`
- Read the new shared-persistence tests in `tests/unit/agents/workflow/test_definition_store.py`
- Ran `uv run pytest tests/unit/agents/workflow/test_api.py tests/unit/agents/workflow/test_definition_store.py tests/unit/agents/workflow/test_advancement.py tests/unit/agents/workflow/test_postgres_persistence.py -q` -> `29 passed`

## Summary
Not LGTM yet. This commit fixes the earlier shared read/list/delete gap, but shared definition versioning is still allocated from process-local cache rather than shared persistence, so the Phase 6 versioned-definition contract remains incomplete.
