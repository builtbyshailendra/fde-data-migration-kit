# Developer tasks. On Windows run them from Git Bash, or call the commands directly.
PYTHON ?= python
VENV   ?= .venv
MAPPING ?= examples/legacy_crm/mapping.yaml
DB     ?= sqlite:///target.db
IMAGE  ?= fde-data-migration-kit:dev

ifeq ($(OS),Windows_NT)
BIN := $(VENV)/Scripts
EXE := .exe
else
BIN := $(VENV)/bin
EXE :=
endif

.PHONY: help venv install lint format test cov demo sample docker clean

help:
	@echo "targets: venv install lint format test cov demo sample docker clean"

$(BIN)/python$(EXE):
	uv venv $(VENV) --python 3.12 || $(PYTHON) -m venv $(VENV)

venv: $(BIN)/python$(EXE)

install: $(BIN)/python$(EXE)
	uv pip install --python $(BIN)/python$(EXE) -e ".[dev,postgres]" || $(BIN)/python -m pip install -e ".[dev,postgres]"

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format:
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

test:
	$(BIN)/pytest -q

cov:
	$(BIN)/pytest -q --cov=migkit --cov-report=term-missing

# The whole quickstart against a throwaway SQLite database.
demo:
	rm -rf out target.db
	$(BIN)/migkit profile --mapping $(MAPPING) --format markdown -o out/profile.md
	$(BIN)/migkit validate -m $(MAPPING) --rejects out/rejects.csv --merges out/merges.csv
	$(BIN)/migkit plan -m $(MAPPING) --db $(DB)
	$(BIN)/migkit load -m $(MAPPING) --db $(DB)
	$(BIN)/migkit reconcile -m $(MAPPING) --db $(DB) --report out/reconciliation.md --json out/reconciliation.json

# Rebuild examples/legacy_crm/invoices.xlsx from scripts/build_sample_workbook.py.
sample:
	$(BIN)/python scripts/build_sample_workbook.py

docker:
	docker build -t $(IMAGE) .

clean:
	rm -rf out target.db .pytest_cache .ruff_cache .coverage htmlcov build dist
