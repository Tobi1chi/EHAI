# Repository Guidelines

## Project Structure & Module Organization

EHAI uses Python for Execution Plane and TypeScript for Control Plane. Put implementations in `src/ehai/` and `control-plane/src/`, with tests in `tests/` and `control-plane/tests/`. Store cross-plane schemas in `schemas/` and documentation in `docs/`.

## Product Scope & Implementation Boundaries

Read `docs/PRODUCT_SCOPE.md` before product or architecture work, then `docs/ROADMAP.md` and the current rebaseline section of `docs/P2_IMPLEMENTATION_PLAN.md`. EHAI is the planning/execution core of a general Agent platform; coding is the first end-to-end use case, not its permanent boundary. The top-level Agent is distinct from Planner. Historical increments and passing tests do not override current scope or prove product delivery. Define each module's user outcome, inputs/outputs, ownership, failure behavior, and normal-entry acceptance before restructuring code. Distinguish target design from implemented interfaces; do not invent commands or status values in usage documentation.

The 2026-09-06 execution model in `docs/EXECUTION_MODEL.md` governs Worker instantiation, phased decision trees, automatic/human Gates, approved-boundary plan adjustments, phase Sessions, handoff recovery, and parallelism. Read it alongside product scope. Keep implementation limitations and historical trial results distinct from these agreed targets.

## Build, Test, and Development Commands

Use `uv`; never use bare `pip`.

- `uv sync` — synchronize the environment.
- `uv run pytest <external-temporary-test.py>` — diagnose a concrete observed failure outside the repository.
- `uv run ruff check .` — lint the project.
- `uv run ruff format --check .` — verify formatting; omit `--check` to apply.

These require `pyproject.toml`; commit `uv.lock` for reproducibility.

Run TypeScript scripts from `control-plane/package.json` with the ADR-selected package manager and lockfile.

## Coding Style & Naming Conventions

Python follows `pyproject.toml`: four spaces, typed public APIs, `snake_case` functions, `PascalCase` classes, and Ruff. TypeScript uses strict mode, `unknown` instead of `any`, `camelCase` functions, and `PascalCase` components/types. Generate cross-plane types from `schemas/`; never duplicate Execution Plane state rules in UI.

## Testing Guidelines

Expose normal CLI/API capabilities first, then agree on one product end-to-end test. That single E2E is the only permanent test code intended for this repository; it has not been authored yet. Legacy unit, integration, contract, smoke, and prototype E2E files are retired, not requirements to restore.

If actual use or the E2E fails, create only the necessary diagnostic test in an external temporary directory and run it with `uv`. After fixing the issue, retry the original failing path. Do not commit temporary tests, copy them into another repository folder, or automatically promote them to regression tests. Do not create speculative tests, test matrices, or a parallel smoke suite. Static lint, formatting, type checks, and client generation/build remain applicable. No collected tests is not a passing product E2E.

## Commit & Pull Request Guidelines

Use Conventional Commits: `type(optional-scope): imperative summary`. Types are `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, `chore`, and `revert`. Keep summaries lowercase, period-free, and within 72 characters, for example `feat(api): add health endpoint`. Explain migration in the body and incompatible changes in a `BREAKING CHANGE:` footer. Keep one logical change per commit.

Before committing, agents must inspect `git status` and `git diff`, stage only task-related files, and run applicable tests and lint checks. Never commit secrets or generated artifacts. Do not amend, rebase, force-push, or push unless the user explicitly requests it.

PRs should explain motivation and approach, list verification, link issues, and flag dependency or configuration changes. Include screenshots for visible changes.

## Security & Configuration

Never commit secrets, `.env` files, virtual environments, generated coverage, or build artifacts. Provide sanitized examples such as `.env.example` and document new variables in `README.md`.
