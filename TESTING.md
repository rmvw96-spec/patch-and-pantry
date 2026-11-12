# Testing

A living record of this repository's test layers, CI timings, and changes to them. Started
2026-11-03; last updated 2026-11-11.

## Current snapshot

| Measure | Template baseline | Now |
| --- | --- | --- |
| Backend tests | 58 | 269 |
| Backend statements measured | 570 | 1,617 |
| Backend coverage | 92% | 96% |
| Backend suite, local run | 16.4 s | 27.2 s |
| Browser E2E specs | 6 files | 7 files (adds `studies.spec.ts`, 6 tests) |

Local times are single runs on one development machine, not CI. Most of the backend increase is
the Prefect test server starting once for the studies API tests.

## CI timings

### Baseline (unmodified template, 2026-11-03)

| Workflow | Trigger | What it runs | Duration |
| --- | --- | --- | --- |
| Test Backend | Push to `master`, PRs | Postgres and Mailpit via Docker Compose, migrations, pytest with coverage | 46 s |
| Test Docker Compose | Push to `master`, PRs | Builds images, runs migrations, starts the stack, checks the health endpoint and frontend | 1 m 48 s |
| Playwright Tests | Push to `master`, PRs, manual; only when `backend/`, `frontend/`, `.env`, compose files, or the workflow change | Browser E2E in 4 parallel shards with `--fail-on-flaky-tests` | 4 m 29 s |

### End of Phase 0 (2026-11-11, `master` at `3e9252f`)

| Workflow | Duration | Change from baseline |
| --- | --- | --- |
| Test Backend | 1 m 15 s | Adds Redis, the migration round trip, the Prefect test server, and 214 tests (was 58) |
| Test Docker Compose | 1 m 56 s | About the same |
| Playwright Tests | 3 m 34 s | Faster, with one more spec file (`studies.spec.ts`) |

The three workflows run in parallel, so a PR waits on the slowest one. Target: PR feedback under
15 minutes.

### After the PRs below

| Workflow | Duration now | Change vs baseline |
| --- | --- | --- |
| Test Backend | 1m 7s | +21s |
| Test Docker Compose | 1m 41s | +7s |
| Playwright Tests | 3m 10s | -1m 19s |

### Other workflows

| Status | Workflows | Why |
| --- | --- | --- |
| Disabled | Deploy (`if: false` in `deploy.yml`) | Deploys to FastAPI Cloud, which this project does not use |
| Removed | Smokeshow | Published the HTML coverage report publicly to smokeshow.helpmanual.io (it ran on 2026-11-03 before being disabled). Test Backend already enforces the 90% gate and keeps the report as the `coverage-html` artifact |
| Kept | Zizmor, Conflict detector | Security lint for workflow files; merge-conflict labels on PRs |
| PR-only | pre-commit (prek), Guard Dependencies | Static checks and client regeneration; blocks dependency changes from non-owners |
| Manual only | Deploy with Docker Compose | Runs on a self-hosted runner when started from the Actions tab |

## Test inventory

| Layer | Location | Size | Notes |
| --- | --- | --- | --- |
| Static checks | `.pre-commit-config.yaml` | 14 hooks | Ruff, strict mypy and `ty` on `backend/app`, Biome, typos, `zizmor`, client generation |
| Backend unit: simulator | `backend/tests/agents/test_simulator.py` | 9 | Known answers plus physics invariants |
| Backend: agent logic, no live LLM | `backend/tests/agents/test_study_agent.py`, `test_demo_model.py` | 16 | `TestModel` / `FunctionModel`; live model calls blocked globally |
| Backend: model factory | `backend/tests/agents/test_models.py` | 20 | Spec parsing, the pinned DeepSeek profile under a custom deployment name, settings over `AZURE_OPENAI_*` variables, v1-endpoint check, no sampling settings, imports without Foundry settings (subprocess), `.env.local` gitignored |
| Backend: live smoke check, offline | `backend/tests/agents/test_smoke.py` | 4 | The tool-call probe catches a tool call written as text; the smoke study reports usage and a trace ID that finds its span |
| Backend: food planner tables | `backend/tests/test_food_models.py` | 53 | Real Postgres, one rolled-back transaction per test: user deletion cascades, CHECK constraints (calorie levels, percentages, dates, closed sets, portion provenance and refuse, capture weights), catalog uniqueness and scope, public-dataset prices, snapshot immutability trigger, seeded region |
| Backend: seed loaders | `backend/tests/test_seed.py`, fixtures in `backend/tests/fixtures/seed/` | 65 | Real Postgres on one connection rolled back at the end (sessions join with savepoints, so even the command line's commits are undone). Idempotency of every loader, ERS price per cup against every row of both committed ERS files (two swapped pairs checked), crosswalk catalog and stand-ins, coverage report, Prefect flow. SR28 piece weights: refuse divided out (known answers), amounts divided down, portion-map validation, zips loaded by content. Miniature FDC and SR28 zips are built in the test |
| Backend: household API | `backend/tests/api/routes/test_household.py`, plus the delete-account test in `test_users.py` | 21 | Created on first access with defaults; budget and members replaced in order; removed members deleted; calorie levels, member count, and budget limits; personal fields refused (`extra_forbidden`), not dropped; users see only their own; the first-access race returns the existing household; deleting the account deletes the household and members |
| Migrations | `.github/workflows/test-backend.yml` | 1 step | Every migration down to an empty database and back up, then `alembic check` |
| Backend integration: studies API | `backend/tests/api/routes/test_studies.py` | 12 | Real Postgres, Redis, and Prefect test harness |
| Backend: agent tracing | `backend/tests/agents/test_tracing.py` | 7 | In-memory exporter that snapshots spans at export time; no Phoenix needed |
| Backend: study job tracing | `backend/tests/test_study_job_tracing.py` | 4 | `execute_study` against real Postgres and the Prefect test harness |
| Backend: template API and CRUD | `backend/tests/api/routes/`, `backend/tests/crud/` | 58 | Items, login, users, private routes, user CRUD |
| Browser E2E: studies | `frontend/tests/studies.spec.ts` | 6 | Full stack in demo mode (`SIM_MODEL=demo`) |
| Browser E2E: template | `frontend/tests/*.spec.ts` | 6 files | Admin, items, login, reset password, sign-up, user settings |
| Stack smoke check | `.github/workflows/test-docker-compose.yml` | 2 HTTP checks | Health endpoint and served frontend |

## Mapping notes


**Fixtures and scope.**
- `db` (session scope, autouse): opens a database session and creates the first superuser. When
  the session ends it deletes all items and users, so every backend test shares one database,
  which is also the development database.
- `client` (module scope): one FastAPI `TestClient` per test file.
- `superuser_token_headers`, `normal_user_token_headers` (module scope): log in through the real
  API. The normal user is created on first use, and gets a fresh random password each module.
- `tests/agents/conftest.py`: a fake simulator that records calls, and the anyio backend.
- `test_studies.py`: a module-scoped Prefect test harness, and model injection through FastAPI
  dependency overrides.
- Tracing tests: an `exporter` fixture installs tracing with a `SnapshotExporter`
  (`tests/utils/tracing.py`) and shuts it down after the test, because agent instrumentation is
  global and must not leak into other tests.
- Playwright: a `setup` project logs in once as the superuser and saves the browser state; other
  specs reuse it, or create their own users through the development-only private API.

**Coverage by layer.** The backend tests cover every API route, including authentication,
permissions, and validation, through the real HTTP layer and a real database. Browser tests cover
the main user journeys: login, sign-up, password reset (through Mailpit), user settings, admin, items,
and studies.

**Gaps.**
- No frontend unit tests: the template has no component test runner, so all frontend behavior is
  checked through the browser.
- Migrations are only tested going up (`prestart.sh` upgrades); nothing in CI checks downgrades.
- The backend tests run background jobs synchronously, so true async execution is untested.
- No load, concurrency, or failure-injection tests.
- No evaluation of real model behavior; everything runs on scripted models.

**Flakiness and speed risks.**
- The shared database, plus deleting all users at session end, ties the backend tests together:
  they cannot run in parallel safely, and a crash mid-run can leave state behind.
- Module-scoped clients and logins couple tests within a file.
- In CI, Playwright retries failures twice, but `--fail-on-flaky-tests` still fails the build if a
  test only passes on retry. That keeps retries from hiding flakiness.
- The first study in a fresh stack waits about 10 seconds for Prefect to start, so E2E studies use
  a 60-second timeout.

## Status of planned additions

| Addition | Layer | Status |
| --- | --- | --- |
| Agent logic tests (`TestModel` / `FunctionModel`) | Agent logic | Done (PR #2) |
| Async studies API: Redis idempotency, Prefect flow, submit → poll → finish | Integration | Done (PR #3) |
| Deterministic demo model (`SIM_MODEL=demo`) | Supports all layers | Done (PR #3) |
| Studies page E2E against the demo-mode stack | Browser E2E | Done (PR #4) |
| Phoenix tracing of study jobs (OpenTelemetry, OpenInference) | Tracing | Done (PR #7) |
| Pydantic Evals golden cases, nightly reference run | Evals and nightly | Next |
| Load and failure tests (Locust, Chaos Mesh) | Resilience | Not planned before the interview |

## How the tests were validated

Each behavior was broken on purpose and the suite rerun; every bug was caught by the test written
for it.

| Deliberate bug | Caught by |
| --- | --- |
| Pressure guard removed | `test_unsafe_pressure_never_reaches_simulator`, `test_retry_message_tells_the_model_why` |
| Usage cap removed from `run_study` | `test_runaway_loop_is_capped` |
| Sub-agent override dropped | `test_wiring_with_test_model` |
| Model errors not translated | `test_malformed_output_becomes_clear_user_error` |
| `Idempotency-Key` ignored | `test_same_idempotency_key_returns_same_study`, `test_key_reserved_by_an_in_flight_request_returns_409` |
| Simulator retries removed | `test_transient_simulator_error_is_retried` |
| Job runner's catch-all removed | `test_unexpected_crash_still_ends_in_a_failed_state` |
| Idempotency keys not scoped per user | `test_idempotency_keys_are_scoped_per_user` |
| Ownership check removed | `test_other_users_cannot_read_a_study` |
| "Feasible" label inverted | `Feasible study shows its result`, `Infeasible study is reported as not feasible` |
| Sidebar link points to the wrong page | `Studies link in the sidebar opens the page` |
| Page stops polling | `Feasible study shows its result` |
| Agent instrumentation not turned on | `test_agent_run_produces_agent_llm_and_tool_spans` and three more |
| OpenInference processor runs after the exporter | `test_agent_run_produces_agent_llm_and_tool_spans` |
| `TRACE_INCLUDE_CONTENT` ignored | `test_content_can_be_left_out_of_spans` |
| Shutdown leaves agents instrumented | `test_shutdown_stops_tracing_and_uninstruments_agents` |
| Tracer used when tracing is off | `test_tracing_is_off_without_an_endpoint`, `test_endpoint_configures_a_batching_otlp_exporter` |
| Agent run outside the `study` span | All four `test_study_job_tracing.py` tests |
| `StudyFailed` not recorded on the span | `test_study_failure_is_recorded_on_the_span` |
| Unexpected crash not recorded on the span | `test_unexpected_crash_is_recorded_on_the_span` |
| Error status not set on failure | `test_study_failure_is_recorded_on_the_span`, `test_unexpected_crash_is_recorded_on_the_span` |
| `study.status` missing on success | `test_study_span_is_the_root_of_the_agent_trace` |
| `study.status` missing for a deleted study | `test_deleted_study_still_ends_its_span` |
| `study.id` missing | `test_study_span_is_the_root_of_the_agent_trace` |
| Household update ignores unknown fields instead of refusing them | All three `test_update_rejects_personal_fields` cases |
| Household update keeps the old members | `test_members_can_be_added_and_removed` and two more |
| Household members accept any integer | Three `test_update_rejects_invalid_values` calorie cases (the database constraint turns them into a 500) |
| First-access race not handled | `test_concurrent_first_access_returns_the_existing_household` |
| SR28 refuse not divided out (edible weight stored as purchased) | `test_purchased_grams_divides_out_refuse` (3 cases), `test_sr28_portions_store_purchased_weight_with_their_arithmetic` |
| Multi-piece measures ("8 pods") not divided by the amount | `test_sr28_portions_store_purchased_weight_with_their_arithmetic` |
| Portion map left out of the snapshot fingerprint | `test_a_changed_portion_map_is_a_new_snapshot` |
| Coverage report ignores portions | `test_report_lists_the_expected_gaps` |
| Owner's refuse override ignored | `test_sr28_portions_store_purchased_weight_with_their_arithmetic` |
| Portion map accepts a duplicate unit and size | `test_portion_map_rejects_bad_rows` (listed twice) |
| `ck_catalog_portion_provenance` dropped in Postgres | `test_owner_measured_portion_records_its_date` |

## Lessons learned

- **A lint fix silently removed a test's setup.** Ruff's unused-argument rule flagged a fixture
  requested only for its side effect; deleting the parameter satisfied the linter, and the
  fixture never ran. Fix: request side-effect fixtures with `@pytest.mark.usefixtures`, or move
  the setup into the test body.
- **Passed locally, failed in CI.** `crypto.randomUUID()` exists only in secure contexts (HTTPS or
  `localhost`). Local runs used `localhost`; CI serves the app at `http://backend:8000`. Fix:
  generate UUIDs with `crypto.getRandomValues()`, which works everywhere. The CI environment is
  the one to trust.
- **A test harness can pass without testing anything.** While checking that the E2E tests catch
  bugs, a failed build left the previous build in place, and the tests ran against it. The fix
  was to stop on build failures, which is the same reason CI gates must fail loudly.
- **An in-memory exporter can hide ordering bugs.** `InMemorySpanExporter` keeps references to
  spans, so attributes a later processor adds still showed up in the test, and swapping the
  OpenInference processor behind the exporter went unnoticed. A real exporter serializes at export
  time. Fix: copy each span on export (`SnapshotExporter`).
- **Tests share the development database.** The template's teardown deletes all users, so rerun
  `scripts/prestart.sh` before logging in after a test run.

## Change log

| Date | Change | Tests | CI effect |
| --- | --- | --- | --- |
| 2026-11-03 | Baseline recorded on the unmodified template | 58 backend | Test Backend 46 s, Test Docker Compose 1 m 48 s, Playwright 4 m 29 s |
| 2026-11-03 | PR #2: agent logic tests; live model calls blocked in all tests | +8 backend | ___ |
| 2026-11-03 | PR #3: async studies API, Prefect simulator, Redis idempotency, demo mode | +29 backend | ___ |
| 2026-11-03 | PR #4: Studies page with browser E2E in demo mode; idempotency keys that work over plain HTTP | +6 E2E | ___ |
| 2026-11-09 | PR #7: OpenTelemetry tracing of study jobs to a local Phoenix | +11 backend | ___ |
| 2026-11-11 | PR #9: Patch & Pantry branding; removed the template's release and `fastapi`-organization workflows; tracing service name is now `patch-and-pantry` | No change (Playwright 68 passed) | Fewer skipped workflow runs; Smokeshow removed (no coverage status on commits; report stays a downloadable artifact) |
| 2026-11-11 | PR #11: model factory for DeepSeek-V3.2 on Foundry (P0-1), `SIM_MODEL` moved into `Settings` (default `demo`), offline-tested smoke check, secrets in `.env.local`. Checked that the profile and lazy-import tests fail with their bugs put back | +24 backend | ___ |
| 2026-11-11 | PR #12: food planner core tables (P0-3) with CHECK constraints and a snapshot immutability trigger; migration round trip in CI. Checked the calorie and cascade tests fail with the constraint dropped in Postgres | +33 backend | Test Backend: one more step (migration round trip) |
| 2026-11-11 | PR #13: seed loaders (P0-4). Tests caught bugs from three put-back defects (no idempotency, no stand-in links, wrong storage preference). Loading the real files found a wrong P0-3 constraint (yield factor capped at 1) and two swapped pairs in ERS's published column. A first version of the flow test committed to the dev database (a default argument captured the real session); fixed, and the stray rows removed | +49 backend | ___ |
| 2026-11-11 | PR #15: Phase 0 close-out. Crosswalk fingerprint on snapshots (+2 backend; the changed-crosswalk test fails when the lookup ignores the fingerprint). Backend suite passes with the real reference data loaded | +2 backend | End of Phase 0, on `master` at `3e9252f`: Test Backend 1 m 15 s (baseline 46 s; adds the migration round trip and the Prefect test server), Test Docker Compose 1 m 56 s (baseline 1 m 48 s), Playwright 3 m 34 s (baseline 4 m 29 s) |
| 2026-11-11 | PR #16: household API (P1-2): `GET` and `PUT /household`. Checked that four put-back bugs each fail their tests (table above) | +21 backend | ___ |
| 2026-11-11 | PR #17: count units (P1-3): `catalog_portion` from SR28 with refuse, `catalog_portions.csv`, capture weights on prices. Owner's refuse override for potatoes (10%). Seven put-back bugs each caught (table above) | +34 backend | ___ |
