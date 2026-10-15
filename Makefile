.PHONY: default init build check lint help
.PHONY: run run_frontend run_cli

SHELL:=/bin/bash

SCRIPTS = ./.claude/skills/sdr-pipeline/scripts
PORT ?= 8787
# No pyproject here — deps come from requirements.txt into a local uv venv
PY = ./.venv/bin/python

# Default target: build the SPA after the checks pass
default: check

init:
	@echo "🏁 python (uv venv + requirements.txt) ..."
	uv venv --allow-existing
	uv pip install -r requirements.txt
	@echo "🏁 frontend ..."
	cd ./webui/frontend/ && npm ci
	@echo "🏁 deck-renderer ..."
	npm ci --prefix ./deck-renderer --no-audit --no-fund

# SPA build (the Dockerfile's stage 1); app.py serves webui/frontend/dist
build: check
	@echo "Building frontend ..."
	cd ./webui/frontend/ && npm run build

# Compile every backend script + the offline self-tests (no network/keys/DB)
check: init
	@echo ""
	@echo "🏁 Compiling ..."
	$(PY) -m py_compile ./webui/server/*.py ./.claude/skills/*/scripts/*.py
	@echo ""
	@echo "🏁 Self-tests ..."
	$(PY) $(SCRIPTS)/tech_signals.py --self-test
	$(PY) $(SCRIPTS)/hiring_signals.py --self-test
	$(PY) $(SCRIPTS)/news_signals.py --self-test
	$(PY) $(SCRIPTS)/suppression.py --self-test
	$(PY) $(SCRIPTS)/csv_audience.py --self-test
	$(PY) ./.claude/skills/ai-sdr/scripts/buyer_group.py --self-test
	$(PY) ./webui/server/user_store.py --self-test

# Not part of `check` yet: the codebase predates ruff and has outstanding findings
lint:
	uvx ruff check ./webui/server/ ./.claude/skills/ ./technographics/src/

# Backend on :$(PORT) (API + built SPA); boots without MONGO_URL/pymongo/dnspython
run:
	env -u PORT $(PY) ./webui/server/app.py --port $(PORT)

# Vite dev server with /api proxied to `make run` on :8787
run_frontend:
	cd ./webui/frontend/ && npm run dev

# support extra arguments: make run_cli script=aisdr_attribution_sync.py args="--dry-run --json"
run_cli:
	$(PY) $(SCRIPTS)/$(script) $(args)

help:
	@echo "Available commands:"
	@echo "  make              - Runs the default 'build' target (check + SPA build)."
	@echo "  make init         - uv venv + requirements.txt, npm ci (frontend, deck-renderer)."
	@echo "  make check        - py_compile + offline self-tests."
	@echo "  make lint         - ruff check (informational, not gating)."
	@echo "  make run          - Runs the console on :$(PORT)."
	@echo "  make run_frontend - Runs the Vite dev server (proxies /api to :8787)."
	@echo "  make run_cli script=<name>.py args=\"...\" - Runs a pipeline script."
