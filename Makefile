.PHONY: eval lint test ci fmt check-deps

PY = python
PYTEST = pytest -q
RUFF = ruff
MYPY = mypy

# Run the full evaluation harness; exits non-zero if any gate fails.
eval:
	$(PY) eval/harness.py

# Run the unit test suite.
test:
	$(PYTEST) tests/

# Lint the codebase.
lint:
	$(RUFF) check app_core eval app.py

# Type check.
mypy:
	$(MYPY) app_core eval app.py

# Format check (ruff format --check).
fmt:
	$(RUFF) format --check app_core eval app.py

# CI: lint, type check, tests, evaluation.
ci: lint mypy test eval

# Dependency sanity: confirm key imports resolve.
check-deps:
	$(PY) -c "import app; print('app imports OK')"
