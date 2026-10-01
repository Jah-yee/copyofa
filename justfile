set quiet := true

# List the available recipes.
_list:
    just --list

# Find dead code
dead-code:
    uv run vulture

# Check for unused or undeclared deps
deps-unused:
    uv run deptry src

# Update deps to latest versions
deps-update:
    uv lock --upgrade
    uv sync --all-groups

# Run formatters
format:
    # pyproject-fmt exits 1 after reformatting; only fail if the file is still off.
    uv run pyproject-fmt pyproject.toml || uv run pyproject-fmt --check pyproject.toml
    uv run ruff format

# Check formatting without modifying files
format-check:
    uv run pyproject-fmt --check pyproject.toml
    uv run ruff format --check

# Run linter
lint:
    uv run ruff check

# Run linter and fix issues
lint-fix:
    uv run ruff check --fix

# Run pre-commit on all files
pre-commit:
    uv run pre-commit run --all-files

# Install pre-commit hooks
pre-commit-install:
    uv run pre-commit install

# Update pre-commit hooks to latest versions
pre-commit-update:
    uv run pre-commit autoupdate --freeze

# Run all quality assurance checks
qa: dead-code deps-unused format-check lint type-check

# Set local dev environment up
setup:
    uv sync --all-groups  # Install dependencies
    uv run pre-commit install  # Install pre-commit hooks
    echo "Run 'source .venv/bin/activate' to activate the Python virtual environment"

# Run tests with coverage
test *args:
    uv run pytest --cov {{ args }}

# Run type checker
type-check:
    uv run mypy
