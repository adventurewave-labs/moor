# Moor — desired-state control plane for Docker environments.
# Demo orchestration: `make demo` runs the full 5-act narrated walkthrough.

SHELL := /bin/bash
COMPOSE := docker compose
CLI := $(COMPOSE) exec -T moor moor

.DEFAULT_GOAL := help

.PHONY: help setup up down reset status plan apply watch mode-auto mode-advise \
	chaos chaos-kill chaos-scale chaos-env chaos-image demo logs test urls clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup: ## Build images and pre-pull all images used by the demo (incl. image-swap chaos)
	$(COMPOSE) build
	$(COMPOSE) pull --ignore-buildable 2>/dev/null || $(COMPOSE) pull db moor-db || true
	docker pull redis:6.2-alpine
	@echo ""
	@echo "  Setup complete. Next:  make demo   (or  make up  to explore manually)"
	@echo ""

up: ## Start the full stack in the background
	$(COMPOSE) up -d
	@$(MAKE) --no-print-directory urls

down: ## Stop the stack (keeps volumes)
	$(COMPOSE) down

reset: ## Full reset: stop the reconciler first, drop volumes, start again
	$(COMPOSE) stop moor || true
	$(COMPOSE) down -v --remove-orphans
	@sleep 2
	$(COMPOSE) up -d
	@$(MAKE) --no-print-directory urls

urls: ## Print all access points
	@echo ""
	@echo "  ────────────────────────────────────────────────"
	@echo "   Moor dashboard .... http://localhost:8080"
	@echo "   Alert sink ........ http://localhost:9099"
	@echo "   Web workload ...... http://localhost:8081"
	@echo "  ────────────────────────────────────────────────"
	@echo ""

status: ## Current compliance snapshot (CLI)
	$(CLI) status

plan: ## Terraform-style drift diff (CLI)
	$(CLI) plan

apply: ## Reconcile now (CLI)
	$(CLI) apply

watch: ## Follow live events (CLI)
	$(CLI) watch

mode-auto: ## Switch reconciler to AUTO mode (remediates drift)
	$(CLI) mode auto

mode-advise: ## Switch reconciler to ADVISE mode (detect + alert only)
	$(CLI) mode advise

chaos: ## Inject one random drift
	$(COMPOSE) run --rm drift-injector inject random

chaos-kill: ## Kill the web container
	$(COMPOSE) run --rm drift-injector inject kill --service web

chaos-scale: ## Scale cache from 1 to 4 rogue replicas
	$(COMPOSE) run --rm drift-injector inject scale --service cache --count 4

chaos-env: ## Mutate the db container environment
	$(COMPOSE) run --rm drift-injector inject env --service db --key POSTGRES_PASSWORD --value rogue

chaos-image: ## Swap the cache image to redis:6.2-alpine
	$(COMPOSE) run --rm drift-injector inject image --service cache --image redis:6.2-alpine

logs: ## Follow moor control-plane logs
	$(COMPOSE) logs -f moor

test: ## Run the control-plane test suite (offline, uses test doubles)
	$(COMPOSE) build moor >/dev/null
	$(COMPOSE) run --rm --no-deps --entrypoint pytest moor tests -q

demo: ## Full narrated end-to-end demo (5 acts, ~3 minutes)
	@bash demo/demo.sh

clean: ## Remove build artifacts and stop everything
	$(COMPOSE) down -v --remove-orphans
