# Review: `phase-4-production-design.md`

## Findings

### 1. Major: the Podman on-demand spawning model creates a much stronger host-control boundary than the plan acknowledges

Phase 4 treats Kubernetes and Podman as two interchangeable deployment targets behind a single switch. That works for many features, but on-demand spawning is not symmetric.

For Kubernetes, the workflow runner gets scoped API access to create Jobs.

For Podman, the workflow runner needs access to the Podman socket in order to create sibling containers. That is effectively host-level container control, which is a much stronger trust boundary than the Kubernetes case.

#### Why this matters

This is primarily a **security** and **functionality** concern:

- the workflow runner becomes able to create/destroy arbitrary containers on the host if misconfigured
- Podman spawning is not just “same feature, different backend”
- the risk profile is materially different from Kubernetes Jobs with scoped ServiceAccounts

The current plan treats Podman as a peer target without explicitly acknowledging that its privilege model is much broader.

#### Recommendation

The plan should explicitly state one of:

- Podman spawning requires a separate trust model and is not equivalent to the Kubernetes production path (deployers should secure the Podman socket appropriately)
- or Podman support for on-demand spawning is deferred behind Kubernetes

### 2. Major: “both targets for every new feature” overstates parity for Kubernetes-specific security controls

The plan says every new Phase 4 feature in the early sub-phases must work on both Kubernetes and Podman. But several items are inherently Kubernetes-native:

- `NetworkPolicy`
- `ServiceAccount` + `RoleBinding`
- TokenReview-based auth
- PVC-backed persistence semantics

At the same time, the deployment switch section says Podman RBAC is effectively a no-op.

#### Why this matters

This is mainly a **quality** and **functionality** problem:

- it creates an unrealistic notion of parity
- it makes acceptance criteria fuzzy
- it can hide the fact that “supported on both targets” does not mean “same guarantees on both targets”

For production-readiness planning, that distinction matters a lot.

#### Recommendation

Clarify the parity contract:

- **behavioral parity** where possible
- **security parity** only where the platform supports it
- explicit “Kubernetes-only hardening” items where Podman has no meaningful equivalent

### 3. Major: the full API-auth design still leaves agent-to-agent authorization too coarse

The plan improves authentication by extending bearer auth to all endpoints and propagating the workflow runner token to spawned agents. But the auth story is still coarse-grained:

- the workflow runner gets broad authority
- spawned agents trust a shared secret or TokenReview validation
- there is no clear per-agent or per-step audience/scope boundary in the plan

#### Why this matters

This is mainly a **security** concern:

- once authenticated, a caller may still be overprivileged
- spawned agents may not distinguish which upstream component is calling them
- token propagation can collapse separation between “who is authenticated” and “what they may do”

The plan introduces per-task permission scoping only in Phase 4c, but some of this risk begins as soon as full API auth and on-demand spawning are introduced.

#### Recommendation

The design should clarify what minimum authorization model exists in Phase 4a/4b:

- shared identity only
- per-agent scopes
- per-endpoint scopes
- inherited identity from workflow runner

If fine-grained auth is deferred, say so explicitly and describe the temporary coarse-grained model.

## Perspective Check

- Functionality: remaining gaps around deployment-target parity and on-demand spawning semantics
- Quality: remaining gap around overclaiming Kubernetes/Podman equivalence
- Security: remaining gaps around Podman socket trust and coarse agent-to-agent authorization

## Open Questions / Assumptions

1. Is Podman on-demand spawning meant to be production-supported, or only a dev/test convenience?
2. Does “support both targets” mean identical guarantees, or only equivalent feature availability where possible?
3. Is the workflow runner intended to hold a broad shared identity in Phase 4, or should some scoped authorization appear earlier than Phase 4c?

## Summary

The Phase 4 design is directionally strong and does a good job turning deferred items from earlier phases into concrete workstreams. The main remaining concerns are not about whether the system should move toward production readiness, but about whether the plan fully distinguishes:

- Kubernetes security/hardening
- Podman dev/test convenience
- authentication vs authorization

If those trust-boundary and parity statements are tightened, the plan will be much easier to implement and much less likely to create false expectations.
