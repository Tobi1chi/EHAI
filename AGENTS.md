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

## Model Tool Changes and Runtime Evidence

Treat a model-facing tool change as a change to the whole calling contract, not just its handler. Read the 2026-09-13 tool integration requirements in `docs/R2_IMPLEMENTATION_PLAN.md` when changing tool definitions, parameters, execution behavior, or result handling.

- Trace the affected path: registered ToolSet and role permissions, provider-facing Schema, prompt/example arguments, handler validation, returned result, and any persistence or downstream consumer. Update affected layers together; do not broaden unrelated interfaces.
- Validate against the configured provider's tool contract, not only general JSON Schema. For strict tools, check object closure and required/nullable fields, including nested objects. Define omission, `null`, empty collections, defaults, and update/preserve semantics explicitly; descriptions and handlers must agree.
- Before claiming a changed tool works, use the normal CLI/API and production assembly in an isolated workspace within the user's current model-call authorization. Confirm provider acceptance, the actual tool arguments and execution, and the relevant persisted result or downstream effect. Acceptance of the ToolSet alone does not verify every handler; an HTTP 200 alone does not prove completion. If unverified, label the capability accordingly.
- For actual call failures, inspect local configuration, serialized requests, Schema and response parsing first. Use a bounded minimal comparison when needed; classify provider, network and authentication causes from evidence. Do not fix unexplained errors by disabling strict validation, silently changing models, or replaying calls with unknown outcomes.
- Follow Testing Guidelines: only failure-driven temporary diagnostics outside the repository, followed by retrying the original normal path. Do not add a permanent tool test suite or speculative verification matrix.
- Record the observed failure, cause, fix and verification scope in the relevant implementation record without secrets. A tool integration trial is not the sole product E2E, and static checks cannot substitute for runtime evidence.

## Commit & Pull Request Guidelines

Use Conventional Commits: `type(optional-scope): imperative summary`. Types are `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, `chore`, and `revert`. Keep summaries lowercase, period-free, and within 72 characters, for example `feat(api): add health endpoint`. Explain migration in the body and incompatible changes in a `BREAKING CHANGE:` footer. Keep one logical change per commit.

Before committing, agents must inspect `git status` and `git diff`, stage only task-related files, and run applicable tests and lint checks. Never commit secrets or generated artifacts. Do not amend, rebase, force-push, or push unless the user explicitly requests it.

PRs should explain motivation and approach, list verification, link issues, and flag dependency or configuration changes. Include screenshots for visible changes.

## Security & Configuration

Never commit secrets, `.env` files, virtual environments, generated coverage, or build artifacts. Provide sanitized examples such as `.env.example` and document new variables in `README.md`.
