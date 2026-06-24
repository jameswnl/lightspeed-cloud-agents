# Review: Phase 6 follow-up commit (`be26775e`)

## Findings

### 1. High: shared definitions are still not actually readable across replicas or restarts
This commit starts persisting submitted definitions into shared workflow state storage, but `DefinitionStore.get()`, `get_version()`, `list_all()`, and `delete()` still read only from the process-local `_definitions` and `_versions` dictionaries. That means a definition submitted on replica A may be written to shared persistence, yet replica B still cannot find it after a restart or load-balancer hop because the read path never consults the shared backend.

Why it matters:
- the headline fix in this commit is only half-implemented
- run-by-name can still fail on another replica even though the definition was "persisted"
- restart durability for definitions is still broken

Recommended fix:
- make definition reads and deletes load from shared persistence, not just in-memory maps
- treat the in-memory maps as a cache at most, not the source of truth

### 2. Medium: the new shared-definition path still lacks regression tests for cross-process behavior
The targeted workflow tests pass, but there are still no tests proving that a definition saved through one `DefinitionStore` instance can be read by a fresh `DefinitionStore` instance backed by the same persistence layer.

Why it matters:
- this exact missing test is why the current half-persistent implementation slipped through
- the claimed shared-definition behavior is still unproven

Recommended fix:
- add tests that create a definition with one store instance and read/list/delete it from a new store instance using the same persistence backend

## Perspective Check
- Functionality: remaining gaps. Snapshot-aware polling looks improved, but shared definitions are still not actually shared on the read path.
- Quality: remaining gaps. The missing cross-instance regression tests left the main seam unverified.
- Security: no new security findings in this commit.

## Verification
- Reviewed commit `be26775e` and the updated `api.py` / `definition_store.py`
- Ran `uv run pytest tests/unit/agents/workflow/test_api.py tests/unit/agents/workflow/test_definition_store.py tests/unit/agents/workflow/test_advancement.py tests/unit/agents/workflow/test_postgres_persistence.py -q` -> `29 passed`

## Summary
Not LGTM yet. This commit moves definitions toward shared persistence, but the read/delete/list paths still use process-local state, so the runner still does not fully satisfy the stateless shared-definition requirement.
