# Repository Guidelines

## Project Structure & Module Organization

EHAI uses Python for Execution Plane and TypeScript for Control Plane. Put implementations in `src/ehai/` and `control-plane/src/`, with tests in `tests/` and `control-plane/tests/`. Store cross-plane schemas in `schemas/` and documentation in `docs/`.

## Product Scope & Implementation Boundaries

`docs/README.md` indexes all documents by role. Read `docs/PRODUCT_SCOPE.md` before product or architecture work, then `docs/ROADMAP.md` (its P2 section covers core closure). For refactoring, follow `docs/REFACTOR_PLAN.md`. `docs/STATUS.md` states what is implemented and verified; `docs/EVIDENCE.md` lists each trial and what it did not cover. EHAI is the planning/execution core of a general Agent platform; coding is the first end-to-end use case, not its permanent boundary. The top-level Agent is distinct from Planner. Historical increments and passing tests do not override current scope or prove product delivery. Define each module's user outcome, inputs/outputs, ownership, failure behavior, and normal-entry acceptance before restructuring code. Distinguish target design from implemented interfaces; do not invent commands or status values in usage documentation.

The current `docs/EXECUTION_MODEL.md` governs Worker instantiation, phased decision trees, automatic/human Gates, approved-boundary plan adjustments, phase Sessions, handoff recovery, and parallelism. Read it alongside product scope. Keep implementation limitations and historical trial results distinct from these agreed targets.

The 2026-09-16 product direction is core-first, incremental platform delivery. Personal Dashboard is a source of selected product ideas, not an inherited full-scope checklist or domain model. Deliver one usable workflow at a time through normal interfaces, then add a thin UI when those capabilities exist. Keep execution ownership in the current core. Fix observed blockers within the selected workflow and retry it; record unrelated findings separately rather than expanding into an open-ended refactor. The user-approved refactor in `docs/REFACTOR_PLAN.md` is the exception: follow its step order, keep each step behavior-preserving, and record pending decisions there instead of resolving them on the side. Do not require all future backend modules before using the platform. Keep roadmap targets separate from implemented capability evidence.

## Build, Test, and Development Commands

Use `uv`; never use bare `pip`.

- `uv sync` — synchronize the environment.
- `uv run pytest tests/test_product_e2e.py` — run the product E2E (no model calls).
- `uv run pytest <external-temporary-test.py>` — diagnose a concrete observed failure outside the repository.
- `uv run ruff check .` — lint the project.
- `uv run ruff format --check .` — verify formatting; omit `--check` to apply.
- `uv run mypy` and `uv run mypy --platform win32` — type-check for Linux and Windows; guard platform-only APIs with `sys.platform`, not `os.name`.

CI (`.github/workflows/ci.yml`) runs these checks, regenerates schemas and the TypeScript client and fails on drift, and runs the product E2E on Linux and Windows.

These require `pyproject.toml`; commit `uv.lock` for reproducibility.

Run TypeScript scripts from `control-plane/package.json` with the ADR-selected package manager and lockfile.

## Coding Style & Naming Conventions

Python follows `pyproject.toml`: four spaces, typed public APIs, `snake_case` functions, `PascalCase` classes, and Ruff. TypeScript uses strict mode, `unknown` instead of `any`, `camelCase` functions, and `PascalCase` components/types. Generate cross-plane types from `schemas/`; never duplicate Execution Plane state rules in UI.

## Testing Guidelines

Expose normal CLI/API capabilities first. The single product E2E is `tests/test_product_e2e.py`: it starts a real `ehai-api` host with the scripted Worker and drives it only through the `ehai` CLI and HTTP API, with no model calls. It is the only permanent test code intended for this repository; extend its one scenario when a refactor or fix needs coverage of the main path, instead of adding new test files. It does not verify Pi, Git worktrees or `integrate-run`; real Pi acceptance stays manual, as described in `docs/REFACTOR_PLAN.md`. Legacy unit, integration, contract, smoke, and prototype E2E files are retired, not requirements to restore; `tests/test_self_hosting.py` is a historic driver.

If actual use or the E2E fails, create only the necessary diagnostic test in an external temporary directory and run it with `uv`. After fixing the issue, retry the original failing path. Do not commit temporary tests, copy them into another repository folder, or automatically promote them to regression tests. Do not create speculative tests, test matrices, or a parallel smoke suite. Static lint, formatting, type checks, and client generation/build remain applicable. The product E2E passing does not prove capabilities it does not exercise.

## Model Tool Changes and Runtime Evidence

Treat a model-facing tool change as a change to the whole calling contract, not just its handler. The requirements below govern tool definitions, parameters, execution behavior, and result handling; `docs/EVIDENCE.md` lists the corresponding runtime evidence; earlier tool-contract records are archived in `docs/history/RECORD_R2.md`.

- Trace the affected path: registered ToolSet and role permissions, provider-facing Schema, prompt/example arguments, handler validation, returned result, and any persistence or downstream consumer. Update affected layers together; do not broaden unrelated interfaces.
- Validate against the configured provider's tool contract, not only general JSON Schema. For strict tools, check object closure and required/nullable fields, including nested objects. Define omission, `null`, empty collections, defaults, and update/preserve semantics explicitly; descriptions and handlers must agree.
- Responses-facing tool and output schemas must omit `uniqueItems` as a project-wide compatibility policy; do not add provider switches or fallback retries for this keyword. Prefer simple model-facing contracts and relax low-value incidental constraints rather than expanding defensive machinery. Preserve necessary authorization, data-integrity and approved acceptance boundaries.
- Before claiming a changed tool works, use the normal CLI/API and production assembly in an isolated workspace within the user's current model-call authorization. Confirm provider acceptance, the actual tool arguments and execution, and the relevant persisted result or downstream effect. Acceptance of the ToolSet alone does not verify every handler; an HTTP 200 alone does not prove completion. If unverified, label the capability accordingly.
- For actual call failures, inspect local configuration, serialized requests, Schema and response parsing first. Use a bounded minimal comparison when needed; classify provider, network and authentication causes from evidence. Do not fix unexplained errors by disabling strict validation, silently changing models, or replaying calls with unknown outcomes.
- Follow Testing Guidelines: only failure-driven temporary diagnostics outside the repository, followed by retrying the original normal path. Do not add a permanent tool test suite or speculative verification matrix.
- Record the observed failure, cause, fix and verification scope without secrets: one row in `docs/EVIDENCE.md`, with the longer narrative in the execution record of the plan driving the work (for example `docs/REFACTOR_PLAN.md`). A tool integration trial is not the sole product E2E, and static checks cannot substitute for runtime evidence.

## Commit & Pull Request Guidelines

Use Conventional Commits: `type(optional-scope): imperative summary`. Types are `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, `chore`, and `revert`. Keep summaries lowercase, period-free, and within 72 characters, for example `feat(api): add health endpoint`. Explain migration in the body and incompatible changes in a `BREAKING CHANGE:` footer. Keep one logical change per commit.

Before committing, agents must inspect `git status` and `git diff`, stage only task-related files, and run applicable tests and lint checks. Never commit secrets or generated artifacts. Do not amend, rebase, force-push, or push unless the user explicitly requests it.

PRs should explain motivation and approach, list verification, link issues, and flag dependency or configuration changes. Include screenshots for visible changes.

## Security & Configuration

Never commit secrets, `.env` files, virtual environments, generated coverage, or build artifacts. Provide sanitized examples such as `.env.example` and document new variables in `README.md`.
