# Cloud Agents — build and run targets

SANDBOX_REPO ?= ../lightspeed-agentic-sandbox
SANDBOX_BRANCH ?= temporal-integration
COMPOSE_FILE = deploy/podman/docker-compose.yaml
DEMO_COMPOSE_FILE = deploy/podman/docker-compose.demo.yaml
export PODMAN_SOCK ?= /run/user/$(shell id -u)/podman/podman.sock

## Quick start: make build up
## Demo:        make demo-up dashboard

# ── Build ──────────────────────────────────────────────

.PHONY: build build-demo build-runner build-sandbox build-mcp build-mcp-kubectl

build: build-runner build-sandbox  ## Build core images (runner + sandbox)

build-demo: build build-mcp build-mcp-kubectl  ## Build all images including MCP servers

build-runner:  ## Build workflow runner image
	podman build -f deploy/workflow-runner/Containerfile -t workflow-runner:latest .

build-sandbox:  ## Build sandbox image (from fork)
	@if [ ! -d "$(SANDBOX_REPO)" ]; then \
		echo "Cloning sandbox fork..."; \
		git clone git@github.com:jameswnl/lightspeed-agentic-sandbox.git $(SANDBOX_REPO); \
	fi
	cd $(SANDBOX_REPO) && git checkout $(SANDBOX_BRANCH) && \
		podman build -f Containerfile -t lightspeed-agentic-sandbox:latest .

build-mcp:  ## Build MCP filesystem server image (demo only)
	podman build -f deploy/mcp-filesystem/Containerfile -t mcp-filesystem:latest .

build-mcp-kubectl:  ## Build MCP kubectl server image (K8s cluster access)
	podman build -f deploy/mcp-kubectl/Containerfile -t mcp-kubectl:latest .

# ── Helpers ────────────────────────────────────────────

.PHONY: ensure-podman

ensure-podman:
	@if ! podman machine inspect >/dev/null 2>&1 || \
		[ "$$(podman machine inspect --format '{{.State}}' 2>/dev/null)" != "running" ]; then \
		echo "Starting Podman machine..."; \
		podman machine start; \
	fi

# ── Run (core) ────────────────────────────────────────

.PHONY: up down restart status logs

up: ensure-podman  ## Start core platform (Temporal + runner)
	podman compose -f $(COMPOSE_FILE) up -d
	@echo ""
	@echo "Services:"
	@echo "  Workflow Runner API: http://localhost:8080"
	@echo "  Temporal UI:        http://localhost:8233"

down:  ## Stop core platform
	podman compose -f $(COMPOSE_FILE) down

restart: ensure-podman  ## Restart core platform
	podman compose -f $(COMPOSE_FILE) down
	podman compose -f $(COMPOSE_FILE) up -d

status:  ## Show running containers
	@podman ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

logs:  ## Show workflow runner logs
	podman logs -f podman-workflow-runner-1

# ── Demo (core + MCP + dashboard) ─────────────────────

.PHONY: demo-up demo-down demo-restart dashboard

demo-up: ensure-podman build-demo  ## Start demo stack (core + MCP server + CORS)
	podman compose -f $(COMPOSE_FILE) -f $(DEMO_COMPOSE_FILE) up -d
	@echo ""
	@echo "Services:"
	@echo "  Workflow Runner API: http://localhost:8080"
	@echo "  Temporal UI:        http://localhost:8233"
	@echo "  MCP Filesystem:     http://localhost:8081"
	@echo ""
	@echo "Run 'make dashboard' to start the demo dashboard."

demo-down:  ## Stop demo stack
	podman compose -f $(COMPOSE_FILE) -f $(DEMO_COMPOSE_FILE) down

demo-restart: ensure-podman  ## Restart demo stack
	podman compose -f $(COMPOSE_FILE) -f $(DEMO_COMPOSE_FILE) down
	podman compose -f $(COMPOSE_FILE) -f $(DEMO_COMPOSE_FILE) up -d

dashboard:  ## Serve demo dashboard at http://localhost:3000
	@echo "Dashboard: http://localhost:3000/demo-dashboard.html"
	cd docs && python3 -m http.server 3000

# ── Kind (Kubernetes) ──────────────────────────────────

KIND_CLUSTER ?= cloud-agents

.PHONY: kind-up kind-down

kind-up: build-demo  ## Create Kind cluster and deploy cloud agents + MCP demo
	KIND_EXPERIMENTAL_PROVIDER=podman kind create cluster --name $(KIND_CLUSTER) --wait 60s
	podman save localhost/workflow-runner:latest -o /tmp/workflow-runner.tar
	KIND_EXPERIMENTAL_PROVIDER=podman kind load image-archive /tmp/workflow-runner.tar --name $(KIND_CLUSTER)
	rm -f /tmp/workflow-runner.tar
	podman save localhost/lightspeed-agentic-sandbox:latest -o /tmp/sandbox.tar
	KIND_EXPERIMENTAL_PROVIDER=podman kind load image-archive /tmp/sandbox.tar --name $(KIND_CLUSTER)
	rm -f /tmp/sandbox.tar
	podman save localhost/mcp-filesystem:latest -o /tmp/mcp-filesystem.tar
	KIND_EXPERIMENTAL_PROVIDER=podman kind load image-archive /tmp/mcp-filesystem.tar --name $(KIND_CLUSTER)
	rm -f /tmp/mcp-filesystem.tar
	podman save localhost/mcp-kubectl:latest -o /tmp/mcp-kubectl.tar
	KIND_EXPERIMENTAL_PROVIDER=podman kind load image-archive /tmp/mcp-kubectl.tar --name $(KIND_CLUSTER)
	rm -f /tmp/mcp-kubectl.tar
	@echo "Tagging images inside Kind node..."
	podman exec $(KIND_CLUSTER)-control-plane ctr --namespace k8s.io images tag \
		localhost/lightspeed-agentic-sandbox:latest docker.io/library/lightspeed-agentic-sandbox:latest
	podman exec $(KIND_CLUSTER)-control-plane ctr --namespace k8s.io images tag \
		localhost/mcp-filesystem:latest docker.io/library/mcp-filesystem:latest
	podman exec $(KIND_CLUSTER)-control-plane ctr --namespace k8s.io images tag \
		localhost/mcp-kubectl:latest docker.io/library/mcp-kubectl:latest
	kubectl apply -f deploy/kind/postgres.yaml
	kubectl wait --for=condition=ready pod -l app=postgres --timeout=60s
	kubectl apply -f deploy/kind/temporal.yaml
	kubectl wait --for=condition=ready pod -l app=temporal-server --timeout=120s
	kubectl create secret generic llm-api-key \
		--from-literal=OPENAI_API_KEY="$$OPENAI_API_KEY" 2>/dev/null || true
	kubectl create secret generic openai-api-key \
		--from-literal=OPENAI_API_KEY="$$OPENAI_API_KEY" 2>/dev/null || true
	kubectl create secret generic anthropic-api-key \
		--from-literal=ANTHROPIC_API_KEY="$$ANTHROPIC_API_KEY" 2>/dev/null || true
	kubectl create configmap demo-data \
		--from-file=examples/demo-data/ 2>/dev/null || true
	kubectl apply -f deploy/kind/rbac.yaml
	kubectl apply -f deploy/kind/network-policy.yaml
	kubectl apply -f deploy/kind/workflow-runner.yaml
	kubectl wait --for=condition=ready pod -l app=workflow-runner --timeout=60s
	kubectl apply -f examples/kind-mcp-filesystem.yaml
	kubectl wait --for=condition=ready pod -l app=mcp-filesystem --timeout=60s
	kubectl apply -f examples/kind-mcp-kubectl.yaml
	kubectl wait --for=condition=ready pod -l app=mcp-kubectl --timeout=60s
	@echo ""
	@echo "Kind cluster '$(KIND_CLUSTER)' ready."
	@echo "Run: kubectl port-forward svc/workflow-runner 8080:8080"
	@echo "Then: curl http://localhost:8080/readyz"

kind-down:  ## Delete Kind cluster
	KIND_EXPERIMENTAL_PROVIDER=podman kind delete cluster --name $(KIND_CLUSTER)

# ── Sandbox log watcher ────────────────────────────────

watch-sandboxes:  ## Watch sandbox container logs (agent loop output)
	@while true; do \
		for c in $$(podman ps --filter label=spawned-by=workflow-runner --format '{{.Names}}' 2>/dev/null); do \
			echo "=== $$c ==="; \
			podman logs -f "$$c" 2>&1 & \
		done; \
		sleep 1; \
	done

# ── Clean ──────────────────────────────────────────────

.PHONY: clean clean-sandboxes

clean-sandboxes:  ## Remove leftover sandbox containers
	@podman rm -f $$(podman ps -a --filter label=spawned-by=workflow-runner --format '{{.Names}}' 2>/dev/null) 2>/dev/null || true
	@echo "Sandbox containers cleaned."

clean:  ## Stop everything and clean up
	-podman compose -f $(COMPOSE_FILE) -f $(DEMO_COMPOSE_FILE) down 2>/dev/null
	-podman compose -f $(COMPOSE_FILE) down 2>/dev/null
	@$(MAKE) clean-sandboxes

# ── Tests ──────────────────────────────────────────────

.PHONY: test test-unit test-load test-multi-replica

test-unit:  ## Run unit tests
	uv run pytest tests/unit/ -q --tb=short

test-load:  ## Run load tests (requires running stack for live mode)
	uv run pytest tests/load/ -v --tb=short

test-multi-replica:  ## Run multi-replica E2E tests (requires Kind + 2 replicas)
	kubectl apply -f deploy/kind/workflow-runner-2-replicas.yaml
	kubectl wait --for=condition=ready pod -l app=workflow-runner --timeout=120s
	uv run pytest tests/e2e/features/steps/test_multi_replica_bdd.py -v --tb=short
	@echo ""
	@echo "Restore single-replica deployment with:"
	@echo "  kubectl apply -f deploy/kind/workflow-runner.yaml"

test: test-unit  ## Run all tests

# ── Help ───────────────────────────────────────────────

.PHONY: help
help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
