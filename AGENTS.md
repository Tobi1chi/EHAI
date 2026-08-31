# Repository Guidelines

## Project Structure & Module Organization

EHAI is a Python scaffold with no package or tests yet. Keep root files limited to configuration and documentation. Add code under `src/ehai/` and mirror it under `tests/` (`src/ehai/services/client.py` maps to `tests/services/test_client.py`). Store fixtures in `tests/fixtures/` and static files in `assets/`.

## Build, Test, and Development Commands

Use `uv` for environments and commands; do not use bare `pip`.

- `uv sync` — create/update the local environment from `pyproject.toml` and `uv.lock`.
- `uv run pytest` — run the complete test suite.
- `uv run pytest tests/path/test_file.py -k test_name` — run a focused test while iterating.
- `uv run ruff check .` — check lint rules.
- `uv run ruff format --check .` — verify formatting; omit `--check` to apply it.

These commands require settings and dependencies in `pyproject.toml`. Commit `uv.lock` for reproducible builds.

## Coding Style & Naming Conventions

Target the Python version in `pyproject.toml`. Use four-space indentation, typed public APIs, and short docstrings for non-obvious behavior. Name modules, functions, and variables with `snake_case`; classes with `PascalCase`; constants with `UPPER_SNAKE_CASE`. Prefer small modules with explicit imports. Keep Ruff configuration in `pyproject.toml`.

## Testing Guidelines

Use `pytest`. Name files `test_*.py` and tests `test_<behavior>`. Cover normal behavior, boundaries, and expected failures; every bug fix needs a regression test. Isolate external services behind fixtures or mocks. No coverage threshold is configured yet.

## Commit & Pull Request Guidelines

Use Conventional Commits: `type(optional-scope): imperative summary`. Allowed types are `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, `chore`, and `revert`. Keep the summary lowercase, omit the final period, and stay within 72 characters. Examples: `feat(api): add health endpoint` and `docs: clarify uv setup`. Use a body to explain rationale or migration steps; mark incompatible changes with a `BREAKING CHANGE:` footer. Each commit must contain one logical change.

Before committing, agents must inspect `git status` and `git diff`, stage only task-related files, and run applicable tests and lint checks. Never commit secrets or generated artifacts. Do not amend, rebase, force-push, or push unless the user explicitly requests it.

Pull requests should explain motivation and approach, list verification commands, link issues, and call out dependency or configuration changes. Include screenshots for user-visible changes.

## Security & Configuration

Never commit secrets, `.env` files, virtual environments, generated coverage, or build artifacts; these are already ignored. Provide sanitized examples such as `.env.example` for required settings and document new variables in `README.md`.
