# Cloud Agents — build and run targets

SANDBOX_REPO ?= ../lightspeed-agentic-sandbox
SANDBOX_BRANCH ?= temporal-integration
COMPOSE_FILE = deploy/podman/docker-compose.temporal.yaml

## Quick start: make build up
## Dashboard: make dashboard → http://localhost:3000/demo-dashboard.html

# ── Build ──────────────────────────────────────────────

.PHONY: build build-runner build-sandbox build-mcp

build: build-runner build-sandbox build-mcp  ## Build all images

build-runner:  ## Build workflow runner image
	podman build -f deploy/workflow-runner/Containerfile -t workflow-runner:latest .

build-sandbox:  ## Build sandbox image (from fork)
	@if [ ! -d "$(SANDBOX_REPO)" ]; then \
		echo "Cloning sandbox fork..."; \
		git clone git@github.com:jameswnl/lightspeed-agentic-sandbox.git $(SANDBOX_REPO); \
	fi
	cd $(SANDBOX_REPO) && git checkout $(SANDBOX_BRANCH) && \
		podman build -f Containerfile -t lightspeed-agentic-sandbox:latest .

build-mcp:  ## Build MCP filesystem server image
	podman build -f deploy/mcp-filesystem/Containerfile -t mcp-filesystem:latest .

# ── Run ────────────────────────────────────────────────

.PHONY: up down restart status logs dashboard

up:  ## Start all services (Temporal + runner + MCP)
	@if ! podman machine inspect >/dev/null 2>&1 || \
		[ "$$(podman machine inspect --format '{{.State}}' 2>/dev/null)" != "running" ]; then \
		echo "Starting Podman machine..."; \
		podman machine start; \
	fi
	podman compose -f $(COMPOSE_FILE) up -d
	@echo ""
	@echo "Services:"
	@echo "  Workflow Runner API: http://localhost:8080"
	@echo "  Temporal UI:        http://localhost:8233"
	@echo "  MCP Filesystem:     http://localhost:8081"
	@echo ""
	@echo "Run 'make dashboard' to start the demo dashboard."

down:  ## Stop all services
	podman compose -f $(COMPOSE_FILE) down

restart:  ## Restart all services
	podman compose -f $(COMPOSE_FILE) down
	podman compose -f $(COMPOSE_FILE) up -d

status:  ## Show running containers
	@podman ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

logs:  ## Show workflow runner logs
	podman logs -f podman-workflow-runner-1

# ── Kind (Kubernetes) ──────────────────────────────────

KIND_CLUSTER ?= cloud-agents

.PHONY: kind-up kind-down

kind-up: build  ## Create Kind cluster and deploy cloud agents
	KIND_EXPERIMENTAL_PROVIDER=podman kind create cluster --name $(KIND_CLUSTER) --wait 60s
	kind load docker-image workflow-runner:latest --name $(KIND_CLUSTER)
	kind load docker-image lightspeed-agentic-sandbox:latest --name $(KIND_CLUSTER)
	kubectl apply -f deploy/kind/temporal.yaml
	kubectl wait --for=condition=ready pod -l app=temporal-server --timeout=120s
	kubectl create secret generic llm-api-key \
		--from-literal=OPENAI_API_KEY="$$OPENAI_API_KEY" 2>/dev/null || true
	kubectl apply -f deploy/kind/rbac.yaml
	kubectl apply -f deploy/kind/workflow-runner.yaml
	kubectl wait --for=condition=ready pod -l app=workflow-runner --timeout=60s
	@echo ""
	@echo "Kind cluster '$(KIND_CLUSTER)' ready."
	@echo "Run: kubectl port-forward svc/workflow-runner 8080:8080"
	@echo "Then: curl http://localhost:8080/readyz"

kind-down:  ## Delete Kind cluster
	KIND_EXPERIMENTAL_PROVIDER=podman kind delete cluster --name $(KIND_CLUSTER)

# ── Dashboard ──────────────────────────────────────────

dashboard:  ## Serve demo dashboard at http://localhost:3000
	@echo "Dashboard: http://localhost:3000/demo-dashboard.html"
	cd docs && python3 -m http.server 3000

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

clean: down clean-sandboxes  ## Stop everything and clean up

# ── Tests ──────────────────────────────────────────────

.PHONY: test test-unit

test-unit:  ## Run unit tests
	uv run pytest tests/unit/ -q --tb=short

test: test-unit  ## Run all tests

# ── Help ───────────────────────────────────────────────

.PHONY: help
help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
