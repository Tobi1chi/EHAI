# Repository Guidelines

## Project Structure & Module Organization

EHAI is a Python scaffold. Keep root files for configuration and docs. Put code in `src/ehai/` and mirror it under `tests/`. Store fixtures in `tests/fixtures/` and static files in `assets/`.

## Build, Test, and Development Commands

Use `uv`; never use bare `pip`.

- `uv sync` — synchronize the environment.
- `uv run pytest` — run all tests.
- `uv run pytest tests/path/test_file.py -k test_name` — run a focused test.
- `uv run ruff check .` — lint the project.
- `uv run ruff format --check .` — verify formatting; omit `--check` to apply.

These require `pyproject.toml`; commit `uv.lock` for reproducibility.

## Coding Style & Naming Conventions

Follow the Python version in `pyproject.toml`. Use four spaces, typed public APIs, and short docstrings where needed. Use `snake_case` for modules, functions, and variables; `PascalCase` for classes; and `UPPER_SNAKE_CASE` for constants. Prefer small modules and explicit imports. Configure Ruff in `pyproject.toml`.

## Testing Guidelines

Use `pytest` with `test_*.py` files and `test_<behavior>` functions. Cover normal behavior, boundaries, and failures; bug fixes require regression tests. Mock external services. No coverage threshold exists yet.

During implementation, run the narrowest relevant test, for example `uv run pytest tests/unit/test_planner.py::test_creates_branch`. Fix failures and rerun that test until it passes. Run `uv run pytest` before committing or final handoff, not after every edit. If the full suite fails, isolate and repair each failing test with focused runs, then rerun the full suite; repeat until it passes.

## Commit & Pull Request Guidelines

Use Conventional Commits: `type(optional-scope): imperative summary`. Types are `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, `chore`, and `revert`. Keep summaries lowercase, period-free, and within 72 characters, for example `feat(api): add health endpoint`. Explain migration in the body and incompatible changes in a `BREAKING CHANGE:` footer. Keep one logical change per commit.

Before committing, agents must inspect `git status` and `git diff`, stage only task-related files, and run applicable tests and lint checks. Never commit secrets or generated artifacts. Do not amend, rebase, force-push, or push unless the user explicitly requests it.

Pull requests should explain motivation and approach, list verification, link issues, and identify dependency or configuration changes. Include screenshots for visible changes.

## Security & Configuration

Never commit secrets, `.env` files, virtual environments, generated coverage, or build artifacts; these are already ignored. Provide sanitized examples such as `.env.example` for required settings and document new variables in `README.md`.
