.DEFAULT_GOAL := help
COMPOSE := docker compose
DEV     := $(COMPOSE) run --rm dev
S       ?= connection-service

.PHONY: help up down build ps logs stop start restart kill metrics stats sniff \
        test lint fmt lock shell clean

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

# --- stack ------------------------------------------------------------------------------------
up: ## Build and start the stack (faults via env, e.g. FAULT_UDP_DROP_PCT=15 make up)
	$(COMPOSE) up -d --build

down: ## Stop the stack (keeps volumes)
	$(COMPOSE) down

build: ## Build images (S=<service> for one; default all)
	$(COMPOSE) build $(if $(filter command line,$(origin S)),$(S))

ps: ## List containers
	$(COMPOSE) ps

logs: ## Follow logs (S=<service>, default all)
	$(COMPOSE) logs -f $(if $(filter command line,$(origin S)),$(S))

stop start restart: ## Stop/start/restart one service (S=<service>)
	$(COMPOSE) $@ $(S)

kill: ## Signal a service (S=<service> SIG=KILL|TERM, default KILL)
	$(COMPOSE) kill -s $(or $(SIG),KILL) $(S)

# --- observe ----------------------------------------------------------------------------------
metrics: ## connection-service metrics (edge_* only)
	@curl -s localhost:9100/metrics | grep -E '^edge_'

stats: ## Backend per-stream stats (count / seq range / gaps)
	@curl -s localhost:8080/stats | python3 -m json.tool

sniff: ## Live packet sniffer in the gateway's netns (VERBOSE=1, PCAP=1)
	@mkdir -p captures
	$(COMPOSE) run --rm --build packet-sniffer

# --- dev --------------------------------------------------------------------------------------
test: ## Run all unit tests
	$(DEV) uv run --all-packages pytest

lint: ## Ruff lint + format check
	$(DEV) sh -c 'uv run ruff check . && uv run ruff format --check .'

fmt: ## Ruff autofix + format
	$(DEV) sh -c 'uv run ruff check --fix . && uv run ruff format .'

lock: ## Re-resolve uv.lock after editing any pyproject.toml
	$(DEV) uv lock

shell: ## Shell in the dev toolbox
	$(DEV) bash

clean: ## Remove containers, volumes and captures
	$(COMPOSE) --profile sniff --profile dev down -v --remove-orphans
	rm -rf captures
