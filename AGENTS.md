# Repository Guidelines

## Project Structure & Module Organization

EHAI uses Python for Execution Plane and TypeScript for Control Plane. Put implementations in `src/ehai/` and `control-plane/src/`, with tests in `tests/` and `control-plane/tests/`. Store cross-plane schemas in `schemas/` and documentation in `docs/`.

## Build, Test, and Development Commands

Use `uv`; never use bare `pip`.

- `uv sync` — synchronize the environment.
- `uv run pytest` — run all tests.
- `uv run pytest tests/path/test_file.py -k test_name` — run a focused test.
- `uv run ruff check .` — lint the project.
- `uv run ruff format --check .` — verify formatting; omit `--check` to apply.

These require `pyproject.toml`; commit `uv.lock` for reproducibility.

Run TypeScript scripts from `control-plane/package.json` with the ADR-selected package manager and lockfile.

## Coding Style & Naming Conventions

Python follows `pyproject.toml`: four spaces, typed public APIs, `snake_case` functions, `PascalCase` classes, and Ruff. TypeScript uses strict mode, `unknown` instead of `any`, `camelCase` functions, and `PascalCase` components/types. Generate cross-plane types from `schemas/`; never duplicate Execution Plane state rules in UI.

## Testing Guidelines

Use `pytest` for Python and `control-plane/package.json` scripts for TypeScript. Cover normal, boundary, and failure cases; bug fixes require regression tests. Mock external services.

During implementation, run the narrowest relevant test and repeat it until fixed. Before committing or final handoff, run the full suites for every affected stack. If a full suite fails, isolate each failure with focused runs, repair it, then rerun all affected suites until they pass. Do not run every test after each edit.

## Commit & Pull Request Guidelines

Use Conventional Commits: `type(optional-scope): imperative summary`. Types are `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, `chore`, and `revert`. Keep summaries lowercase, period-free, and within 72 characters, for example `feat(api): add health endpoint`. Explain migration in the body and incompatible changes in a `BREAKING CHANGE:` footer. Keep one logical change per commit.

Before committing, agents must inspect `git status` and `git diff`, stage only task-related files, and run applicable tests and lint checks. Never commit secrets or generated artifacts. Do not amend, rebase, force-push, or push unless the user explicitly requests it.

PRs should explain motivation and approach, list verification, link issues, and flag dependency or configuration changes. Include screenshots for visible changes.

## Security & Configuration

Never commit secrets, `.env` files, virtual environments, generated coverage, or build artifacts. Provide sanitized examples such as `.env.example` and document new variables in `README.md`.
