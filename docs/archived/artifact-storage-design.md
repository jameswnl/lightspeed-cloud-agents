# Agent Artifact Storage & Distribution Design

## Problem

Agent artifacts (agent.yaml, tool modules, skills, registry) need to be available inside ephemeral pods at runtime. The current approach uses ConfigMaps (K8s) and host volume mounts (Podman), which works for PoC but has limitations:

- ConfigMaps have a ~1MB size limit
- Volume mounts require host-level file management
- No versioning or immutability guarantees
- No standard distribution mechanism across clusters

## Current State (PoC)

| Artifact | K8s | Podman |
|----------|-----|--------|
| agent.yaml | ConfigMap | Host volume mount |
| tools/*.py | ConfigMap | Host volume mount |
| skills/ | Volume/PVC | Host volume mount |
| registry.yaml | ConfigMap | Host volume mount |

## Options for Production

### Option 1: Derived Images (recommended for simple cases)

```dockerfile
FROM agent-runtime:latest
COPY my-agent.yaml /app/agent.yaml
COPY my_tools/ /app/tools/
COPY my_skills/ /app/skills/
```

- **Pros:** No mounts needed, fully immutable, versioned via image tags, works on both K8s and Podman
- **Cons:** Requires image rebuild per agent change, larger image registry footprint
- **Best for:** Stable agents that don't change frequently

### Option 2: OCI Artifact Registry

Store agent definitions as OCI artifacts (not container images). Agent runtime pulls them at startup via an init container or entrypoint hook.

```yaml
# In agent deployment or workflow step config
artifacts:
  - oci://registry.example.com/agents/diagnostic:v1.2
```

- **Pros:** Kubernetes-native, versioned, immutable, reuses existing registry infrastructure
- **Cons:** Requires OCI artifact tooling, adds startup latency for pull
- **Best for:** Large-scale multi-cluster deployments

### Option 3: PVC / Shared Volume

Store artifacts in a persistent volume. Pods mount the volume read-only.

- **Pros:** Simple, no image rebuild, supports large files (skills directories)
- **Cons:** Single point of failure, not immutable, requires volume provisioning
- **Best for:** Podman deployments, single-cluster setups

### Option 4: Git-sync Sidecar

A sidecar container pulls agent config from a git repo on startup and keeps it synced.

- **Pros:** Git-native versioning, familiar workflow, supports any file size
- **Cons:** Adds complexity (sidecar), requires git repo access, sync latency
- **Best for:** GitOps-driven deployments

## Registry Design

The current `registry.yaml` is a static name→endpoint map. For production:

| Concern | Current | Production |
|---------|---------|-----------|
| Discovery | Static YAML file | K8s Service discovery or service mesh |
| Updates | Requires pod restart | Watch-based or DNS-based live updates |
| Health | No health awareness | Registry checks agent /healthz before routing |
| Ephemeral pods | Spawner returns endpoint directly | Registry becomes optional for ephemeral steps |

For ephemeral-by-default workflows, the registry is mainly useful for:
- Pre-deployed agents (if any steps use `spawn: pre-deployed`)
- Cross-workflow agent sharing
- Administrative visibility (list all available agents)

## Recommendation

**Phase 6 implementation order:**

1. **Derived images** — simplest path, document the pattern, provide a Containerfile template
2. **OCI artifacts** — for multi-cluster production deployments
3. **Registry enhancement** — K8s Service discovery, health-aware routing

ConfigMaps and volume mounts remain supported for development and simple deployments.
