# Testing Strategy

Principle: **nothing is "done" until the relevant tests have actually run and passed**, and results in
`PROJECT_PROGRESS.md` are copied from real runs, never written by hand.

## Layers

| Layer | Tooling | Location | Runs |
|---|---|---|---|
| Python unit | pytest | `tests/unit` | every change |
| Python integration | pytest + FastAPI TestClient + real SQLite + real git repos | `tests/integration` | every change |
| API | OpenAPI-driven checks | `tests/integration/test_api_*` + API Testing Agent | every change |
| Database | Alembic upgrade/downgrade, metadata-vs-migration diff, integrity | `tests/integration/test_migrations.py` + DB Testing Agent | every change |
| Security | bandit, secret-scan corpus, path-traversal / permission tests, pip-audit/npm audit | `tests/security` | every change / release |
| Regression | baseline pass-set comparison | `tests/regression` + Regression Agent | every change |
| Performance | indexing/search/latency budgets | `tests/performance` | phase end |
| Extension unit | Vitest (pure logic, mocked `vscode` only where unavoidable) | `vscode-extension/test/unit` | every change |
| Extension host | `@vscode/test-electron` against real VS Code | `vscode-extension/test/suite` | phase end |
| Webview E2E | Playwright (Chromium) with a host shim against a live backend | `tests/e2e` | phase end |
| Lint/types | ruff, mypy (backend), tsc + ESLint (extension) | CI + Code Quality Agent | every change |

## Dedicated testing agents (runtime, not just CI)

The ten testing agents in `backend/app/testing/agents/` run *against a target workspace* inside the
orchestrated pipeline. Each defines: responsibility, how it discovers/executes tests, pass/fail criteria,
and emits a `TestReport` of `TestCase` records with: name, description, expected, actual, status, error
details, correction applied, retest outcome.

| # | Agent | Executes | Passes when |
|---|---|---|---|
| 1 | Unit | detected unit-test command (pytest / vitest / jest / phpunit) | exit 0, ≥1 test collected, 0 failures |
| 2 | Integration | integration subset | same |
| 3 | End-to-End | e2e subset (Playwright/pytest e2e) | same; *skipped* (never "passed") if none exist |
| 4 | API | OpenAPI-driven probing of a FastAPI app in-process | no 5xx, responses match declared schemas, auth enforced |
| 5 | Database | migrations up/down, ORM↔migration drift, FK + integrity checks | all checks clean |
| 6 | Extension | `tsc --noEmit`, manifest↔code command parity, Vitest | all clean |
| 7 | Security | bandit, secret scan, dependency audit, custom scanner | no findings ≥ configured severity |
| 8 | Performance | micro-benchmarks vs budgets | all within budget |
| 9 | Regression | compares current pass-set to stored baseline | no previously-passing test now failing |
| 10 | Code Quality | ruff / eslint / complexity | no errors |

Honesty rules baked into the framework: a missing tool or no tests ⇒ status `skipped` with a reason, never
`passed`; a command that cannot be launched ⇒ `error`; the orchestrator's verification gate requires ≥1
executed (non-skipped) relevant agent.

## Module completion checklist (applied per module)
1. Test plan (in the module's test file docstring) → 2. unit tests → 3. run → 4. integration tests →
5. security checks → 6. behaviour verified in the running app → 7. results recorded in PROJECT_PROGRESS.md.


## Results of the final run (2026-11-17)

`scripts/check.sh --host --package` → exit 0: pytest 601 passed (87.4 % coverage) · performance 5 · Playwright E2E 42 · Vitest 141 · extension host 6 ·
clean-profile VSIX install 5. Linters/type-checkers: ruff, mypy (111 files), bandit, tsc, ESLint — clean. Layout: `tests/{unit,integration,security,regression,performance,e2e}`,
`vscode-extension/test/{unit,integration,suite,packaged}`.

Failures of the Playwright suite leave evidence (screenshot, page state, failed network requests) in `test-results/FAILED-*`; the harness's HTTP servers use a listen backlog of 128 (the default of 5 reset connections under bursts).

Honest gaps: no test ran against a **real** LLM (`tests/e2e/test_live_ollama.py` is opt-in and skipped); the Ollama client and generation pipeline are verified with protocol doubles
(`tests/helpers/fake_ollama.py`), which prove protocol handling and PatternVault's logic, not model quality.
