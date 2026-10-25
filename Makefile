PYTHON ?= python3
VENV   ?= .venv
BIN    := $(VENV)/bin

.DEFAULT_GOAL := help
.PHONY: help venv install lint format test check clean

help: ## Show available targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-10s %s\n", $$1, $$2}'

venv: $(BIN)/python ## Create the virtualenv

$(BIN)/python:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip

install: venv ## Install the package in editable mode with dev extras
	$(BIN)/pip install -e '.[dev]'

lint: ## Run ruff lint and format checks
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format: ## Apply ruff formatting and safe fixes
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

test: ## Run the test suite (offline; AWS is mocked with moto, GCP with fakes)
	$(BIN)/pytest -q

check: lint test ## Lint and test (what CI runs)

clean: ## Remove caches and build artefacts
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
