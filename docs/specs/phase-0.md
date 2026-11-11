# Phase 0 spec: Foundations

Status: **draft for owner review** (2026-11-11). Roadmap: [`../roadmap.md`](../roadmap.md).
Design: [`../design.md`](../design.md). Known answers for tests: [`known-answers.md`](known-answers.md).

## Goal

Lay the food domain's data foundations and connect Claude on Microsoft Foundry, without changing
any user-visible behavior. The Studies feature stays as it is (it is replaced at the start of
Phase 1), so the platform's patterns remain covered by the existing tests.

**Out of scope:** new pages, new agent behavior, the AMS weekly flow (Phase 2), automated NOAA
pulls (the region row is seeded by hand from `design.md`), and anything that calls a live model
in CI.

## Prerequisites (owner)

- [ ] Azure budget with email alerts at 50% and 90% of 200 USD.
- [ ] Microsoft Foundry deployments of `claude-sonnet-5-5` and `claude-haiku-5-5`, ideally with
      those exact deployment names.
- [ ] Local `.env` values (never committed): `ANTHROPIC_FOUNDRY_RESOURCE` (or
      `ANTHROPIC_FOUNDRY_BASE_URL`) and `ANTHROPIC_FOUNDRY_API_KEY`.

## P0-1: Model factory

**Where:** new `backend/app/agents/models.py`; `backend/app/core/config.py`;
`backend/pyproject.toml`; `backend/app/agents/study_agent.py`.

- Add the Anthropic extra: `pydantic-ai-slim[openai,anthropic]`.
- Add settings (read from `.env` by `Settings`): `ANTHROPIC_FOUNDRY_RESOURCE: str | None`,
  `ANTHROPIC_FOUNDRY_BASE_URL: str | None`, `ANTHROPIC_FOUNDRY_API_KEY: SecretStr | None`, and
  move `SIM_MODEL` into `Settings` (default `"demo"`; it is currently read from `os.environ` at
  import time, which `CLAUDE.md` documents as a gotcha).
- `build_model(spec: str) -> Model | str`:

  | `spec` | Returns |
  | --- | --- |
  | `demo` | The existing deterministic scripted model |
  | `test` | `"test"` (Pydantic AI `TestModel`) |
  | `foundry:<canonical>` | `AnthropicModel(<canonical>, provider=..., profile=anthropic_model_profile(<canonical>))` |
  | `foundry:<canonical>@<deployment>` | Same, but the request uses `<deployment>` as the model name |
  | anything else | `ValueError` naming the allowed forms |

  `<canonical>` must be `claude-sonnet-5-5` or `claude-haiku-5-5`. The provider is
  `AnthropicProvider(anthropic_client=AsyncAnthropicFoundry(resource=... or base_url=...,
  api_key=...))`.
- **Always pass `profile=anthropic_model_profile(<canonical>)`.** Pydantic AI looks profiles up by
  model name; with a custom deployment name and no explicit profile, Sonnet 5.5 loses the
  forced-tool-use fallback and every structured-output request fails (verified 2026-11-11 with
  `anthropic` 2.8.0 and Pydantic AI 2.65).
- Never set `temperature`, `top_p`, or `top_k` in model settings.
- **Build lazily.** Importing any `app` module must not require Foundry settings or construct a
  network client. Resolve the model when a run starts (`run_study` already accepts `model=`).
  A `foundry:` spec with missing settings raises a clear error naming the three variables.

**Acceptance criteria:**

- [ ] Unit tests (no network): `foundry:claude-sonnet-5-5` yields an `AnthropicModel` whose
      profile has `supports_forced_tool_choice == False`; the same holds for
      `foundry:claude-sonnet-5-5@my-custom-name`; `foundry:claude-haiku-5-5` yields `True`;
      `demo` and `test` behave as today; unknown specs raise `ValueError`.
- [ ] A test asserts no model settings set `temperature`, `top_p`, or `top_k`.
- [ ] The full suite passes with no Foundry variables set.
- [ ] `scripts/smoke_foundry.py` (manual, never in CI) runs one tiny study through each Foundry
      model with tracing on and prints the trace ID and token usage.

## P0-2: Measure tokens

- [ ] With `docker compose up -d phoenix` and `PHOENIX_COLLECTOR_ENDPOINT` set, run the smoke
      script for both models.
- [ ] Record input, output, and cache tokens per run, plus the run's request count, in
      `design.md`'s budget section; replace the assumed 7,500 / 600 figures and recompute the
      cost table. Add a change-log row.

## P0-3: Core tables

**Where:** `backend/app/models.py` (follow the `Study` patterns: UUID primary keys,
timezone-aware timestamps, `Literal` types for closed sets, cascading deletes from `user`), one
Alembic revision. Money is **integer cents** (proposed; confirm in review).

| Table | Fields | Notes |
| --- | --- | --- |
| `data_snapshot` | `id`, `source` (Literal: `ers_prices`, `fdc_foundation`, `fdc_sr_legacy`, `foodkeeper`, `planting_calendar`, `garden_yields`, `noaa_normals`, `ams_retail`, `dga`, `fda_daily_values`), `version`, `retrieved_at`, `url`, `sha256`, `row_count`, `notes` | Unique (`source`, `sha256`); rows never updated or deleted |
| `region` | `id`, `code` (unique, `north-al`), `name`, `noaa_station`, `ams_region`, `frost_risk_pct` (default 30), `last_spring_frost` (`MM-DD`), `first_fall_frost` (`MM-DD`), `frost_snapshot_id` | Seed one row: Muscle Shoals 2 N, Southeast, 30, `04-03`, `10-30` |
| `household` | `id`, `owner_id` (unique, FK user, cascade), `region_id`, `user_setting` (Literal: `store_only`, `plus_local`, `plus_growing`; default `store_only`), `produce_budget_cents_per_week` (nullable), `local_premium_pct` (nullable, 0-100), `created_at` | One household per user for now |
| `household_member` | `id`, `household_id` (cascade), `calorie_level` (default 2000; one of 1000, 1200, ..., 3200), `created_at` | **No other fields, by design** (no names, ages, sex, or body data) |
| `catalog_item` | `id`, `slug`, `name`, `kind` (Literal: `produce`, `supply`), `category`, `scope` (Literal: `national`, `region`, `household`), `region_id` (nullable), `household_id` (nullable, cascade), `preparation_yield_factor`, `cup_equivalent_lb`, `storage_days_min`, `storage_location`, `stand_in_for_id` (nullable self-FK), `conversion_source` (required text), `snapshot_id` (nullable) | Unique (`slug`, `scope`, `region_id`, `household_id`); lookups resolve household, then region, then national |
| `catalog_nutrient` | `id`, `catalog_item_id`, `nutrient` (FDC nutrient name or number), `amount_per_100g`, `unit`, `snapshot_id` | From FoodData Central |
| `crop_profile` | `id`, `catalog_item_id`, `yield_lb_per_sq_ft`, `yield_source`, `days_to_maturity_min`, `days_to_maturity_max`, `snapshot_id` | Minnesota yields first |
| `planting_window` | `id`, `catalog_item_id`, `region_id`, `season` (Literal: `spring`, `fall`), `start_md`, `end_md`, `verified` (default `False`), `snapshot_id` | Derived North Alabama dates load with `verified=False` |
| `vendor` | `id`, `household_id` (cascade), `name`, `vendor_type` (Literal: `store`, `farmers_market`, `farm_share`, `local_grower`, `supplier`), `region_id`, `area` (city or area; **never a street address**) | Private to the household |
| `price_observation` | `id`, `household_id` (nullable = public dataset; cascade), `catalog_item_id`, `vendor_id` (nullable), `price_cents`, `quantity`, `unit` (as written, e.g. `3 lb bag`), `grams_edible` (nullable; filled by `normalize_price` in Phase 1), `observed_on`, `captured_via` (Literal: `typed`, `photo`, `receipt`, `public_dataset`), `source_type` (Literal: `store`, `farmers_market`, `farm_share`, `local_grower`, `public_data`), `region_id` (nullable for national data), `visibility` (Literal: `private`, `shared`; default `private`), `is_sale_price` (default `False`), `raw_text` (the typed line; never an image), `snapshot_id` (nullable), `created_at` | |

**Acceptance criteria:**

- [ ] Migration upgrades and downgrades cleanly (round trip).
- [ ] Deleting a user deletes their household, members, vendors, household-scoped catalog items,
      and prices (test).
- [ ] Invalid calorie levels and percentages are rejected (test).
- [ ] The North Alabama region row exists after `prestart.sh`.

## P0-4: Seed loaders

**Where:** new package `backend/app/seed/`, Prefect flows run by hand
(`uv run python -m app.seed <loader> <path>`), tiny fixtures in `backend/tests/fixtures/seed/`.

- `register_snapshot(...)` hashes the file (SHA-256) and returns the existing snapshot when
  (`source`, `sha256`) already exists, so loading the same file twice changes nothing.
- A **crosswalk** file, `backend/app/seed_data/catalog_crosswalk.csv`, maps each catalog slug to
  its names in each source (ERS item and form, FDC id, FoodKeeper id, Minnesota crop name,
  planting-chart crop name). No fuzzy matching; the owner reviews the crosswalk.
- Loaders take a local file path (downloads are a separate manual step, documented in
  `backend/app/seed_data/README.md`):

  | Loader | Input | Writes |
  | --- | --- | --- |
  | `load_ers_prices` | ERS vegetable and fruit CSVs | National produce catalog items (conversion fields from the Fresh form; Dried for beans) and public-data price observations |
  | `load_planting_calendar` | `north_alabama_planting_calendar.csv` | `planting_window` rows, `verified=False` |
  | `load_garden_yields` | `minnesota_home_garden_yields.csv` | `crop_profile` rows |
  | `load_foodkeeper` | FoodKeeper JSON | `storage_days_min` (range minimum) and location |
  | `load_fdc_subset` | FDC Foundation (fallback SR Legacy) download | `catalog_nutrient` rows for the first crop set |

- **First crop set** (slugs): tomato, sweet-pepper, hot-pepper, snap-bean, cucumber, zucchini,
  yellow-squash, collards, kale, turnip-greens, mustard-greens, lettuce, spinach, swiss-chard,
  onion, okra, cabbage, carrot, broccoli, cauliflower, radish, beet, turnip, eggplant,
  sweet-potato, potato, butternut-squash, acorn-squash, sweet-corn, field-corn, black-bean,
  pinto-bean, lima-bean, southern-pea, garden-pea, peanut, watermelon, cantaloupe. Supplies:
  topsoil-bag, topsoil-bulk, compost, cedar-1x8x8, cedar-2x8x8, seed-packet.

**Acceptance criteria:**

- [ ] Each loader run twice on the same file creates one snapshot and no duplicate rows.
- [ ] The ERS loader's computed price per cup matches ERS's published column within 0.001 USD for
      every row (see known answers).
- [ ] Every catalog item has a `conversion_source`; stand-ins point at the item they borrow from.
- [ ] A report command lists first-crop-set items missing a price, conversion, nutrients, or
      storage time, matching the gaps in `design.md` ("ERS coverage").
- [ ] Loader tests use fixtures only; no network in CI.

## P0-5: Close out

- [ ] Tick the Phase 0 tasks in `roadmap.md`; update `design.md` and its change log.
- [ ] `TESTING.md`: new test files and a change-log row with CI timings.
- [ ] Write `docs/specs/phase-1.md` for owner review.

## Gate

Migrations round-trip; seed loads are repeatable and versioned; CI is green with scripted models
and no Foundry settings; one live Foundry run per model appears in Phoenix with token counts, and
`design.md`'s budget section uses measured numbers.

## Decisions for the owner's review

| Decision | Default in this spec |
| --- | --- |
| Money representation | Integer cents |
| `SIM_MODEL` default | `demo` (live models only when set explicitly) |
| National data rows | `region_id` nullable rather than a "national" region row |
| Crosswalk review | The owner approves `catalog_crosswalk.csv` before loaders depend on it |
