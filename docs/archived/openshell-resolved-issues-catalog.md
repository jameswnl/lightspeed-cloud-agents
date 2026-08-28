# OpenShell integration — resolved issues catalog

Catalog of real bugs found in `OpenShellSpawner` and its tests while verifying
against **real** OpenShell gateways (not mocks). All entries below are fixed
and merged to `main`. Kept for pattern-matching if a similar bug resurfaces —
if you're actively debugging `OpenShellSpawner`, skim this before re-deriving
root causes from scratch.

For open/unfixed gaps, see the GitHub issue tracker instead of this doc
(e.g. #209 HTTP-exposure readiness gap, #214 orphaned-provider cleanup
ordering) — once something has a filed issue, it doesn't need duplicate
tracking here.

## Headline lesson

Every `OpenShellSpawner` method that was only ever tested against mocked gRPC
stubs shipped with real bugs — wrong protobuf message classes, wrong field
names, wrong id-vs-name semantics. `mocker.Mock()`/`MagicMock()` auto-create
any attribute access, so a wrong `.provider.id` or a wrong module
(`openshell_pb2.Provider` instead of `datamodel_pb2.Provider`) never fails a
mocked test — it only fails against the real `openshell` SDK. See
`docs/testing-against-openshell-gateways.md` for how to verify against a real
gateway before trusting a spawner change.

## Protobuf module / field-naming bugs (2026-08-27, PRs #212, #213)

- **Wrong generated module for domain types.** `openshell._proto.openshell_pb2`
  has request/response wrapper messages (`CreateProviderRequest`,
  `AttachSandboxProviderRequest`, etc.) but **not** the domain types they
  embed. Domain types (`Provider`, `ObjectMeta`, `ProviderResponse`) live in a
  separate generated module, `openshell._proto.datamodel_pb2`. Code that did
  `openshell_pb2.Provider(...)` raised `AttributeError` only against the real
  SDK, never against a `MagicMock()`. If unsure which module a field's type
  lives in, check `SomeRequest.DESCRIPTOR.fields_by_name['x'].message_type.full_name`
  / `.file.name`.
- **No top-level id/name on `Provider`.** Both live under `Provider.metadata`
  (an `ObjectMeta`: `id, name, created_at_ms, labels, resource_version,
  annotations, workspace, deletion_timestamp_ms`). Code that read
  `provider.id` directly was wrong; needed `provider.metadata.id`.
- **Providers must be cross-referenced by `.metadata.name`, not `.metadata.id`**,
  everywhere: `SandboxSpec.providers` (repeated string),
  `AttachSandboxProviderRequest.provider_name`,
  `DetachSandboxProviderRequest.provider_name`. Passing the id produced a
  real, misleading-looking error — `FAILED_PRECONDITION: provider '<id>' not
  found` — that looked like a workspace/auth problem but was just the wrong
  identifier.
- **`DeleteProviderRequest`'s field is `name`** (not `provider`), and
  empirically accepts either the id or the name as that "name" value (looser
  than Attach/Detach/spec.providers, which strictly require the name) — don't
  assume this means id-based lookup works everywhere else.
- **Missing `workspace=` field.** `AttachSandboxProviderRequest`/
  `DetachSandboxProviderRequest`/`CreateProviderRequest` all need an explicit
  `workspace=`. Omitting it doesn't error immediately — it causes
  `CreateSandbox`/lookups to fail later with confusing "not found" errors
  that don't point back to the missing field.
- **Test-pollution in `test_openshell_spawner.py`.** The file stubs `openshell`
  with a `MagicMock` at import time when the real package isn't installed
  (CI doesn't install the `openshell` extra). Tests needing real protobuf
  objects must swap the stub out via the shared `_real_openshell_modules()`
  context manager (module-level in that file) — a naive version that only
  restores originally-mocked `sys.modules` keys leaves stray real submodules
  behind and corrupts *other* tests later in the same file when run together
  locally (silent in CI, since these tests skip there entirely — part of why
  the underlying bugs shipped unnoticed).

## Credential exposure fix chain (issue #199, PR #208)

`credential_secret_name`-based LLM credential injection via `OpenShellSpawner`
went through several review rounds before it actually worked end-to-end
without exposing the real credential value anywhere in the sandbox:

- A `server_env` leak (credential landing somewhere it'd be visible outside
  the intended provider-injection path).
- A TLS fail-open regression (a code path that should have refused to send
  credentials over a non-TLS channel but didn't).
- A Kubernetes-PodSpec leak introduced by a Podman-specific accommodation
  (a fix for the Podman driver inadvertently exposed the credential in the
  Kubernetes driver's PodSpec).

None of this was visible from the diff alone — each was only caught by
running the fix against a real gateway. Confirmed fail-closed behavior: a
plaintext (non-TLS) gateway now refuses provider creation with credentials
(`RuntimeError: Provider creation requires TLS (OPENSHELL_TLS_CA) — refusing
to send credentials over insecure channel`) unless the caller opts in via
`OPENSHELL_ALLOW_INSECURE_CREDENTIALS=1` for known-plaintext dev gateways.

## Landlock materialize-skills permission bug (issue #202)

The `allowed_skills` scoping fix granted `/app` `read_only` but never
`read_write` on `/app/skills`, the actual `materialize-skills.sh` write
destination. The sandbox image's own `chmod -R g+w` looked correct in
isolation and passed an earlier Podman-only permission test, but Landlock
denies writes independently of POSIX permissions it hasn't been told to
allow. Two independent code reviews (careful about exec ordering, argv
injection safety, validation placement) both called the change "sound, no
blocking issues" — only a live spawn against a real gateway surfaced
`materialize-skills.sh` failing with `Permission denied`. No mocked
`exec_stream()` test, and no amount of reading the Python logic, can catch an
LSM-level kernel denial — only a real kernel enforces Landlock.

Fixed by adding the `read_write` grant on `_MATERIALIZED_SKILLS_DIR`
(`/app/skills`), scoped to only apply when `allowed_skills` is set (not
unconditional, not on `/app` broadly). See `openshell-skills-redesign-202`
project memory for the full architecture this fix is part of.
