# Phase 0 spec: Foundations

Status: **done** (2026-11-11), except the budget item moved to Phase 1 (see Gate). Roadmap: [`../roadmap.md`](../roadmap.md).
Design: [`../design.md`](../design.md). Known answers for tests: [`known-answers.md`](known-answers.md).

## Goal

Lay the food domain's data foundations and connect DeepSeek-V3.2 on Microsoft Foundry, without
changing any user-visible behavior. The Studies feature stays as it is (it is replaced at the start
of Phase 1), so the platform's patterns remain covered by the existing tests.

**Out of scope:** new pages, new agent behavior, the AMS weekly flow (Phase 2), automated NOAA
pulls (the region row is seeded by hand from `design.md`), and anything that calls a live model
in CI.

## Prerequisites (owner)

- [x] Azure budget with email alerts at 50% and 90% of 200 USD.
- [x] Microsoft Foundry deployment of DeepSeek-V3.2 (the model sold by Azure), named
      `DeepSeek-V3.2`. Claude could not be deployed: it needs an Enterprise Agreement or MCA-E
      subscription (see `design.md`, Model, budget, and integration).
- [ ] Local `.env.local` values (gitignored; `.env` is committed, so secrets never go there):
      `FOUNDRY_ENDPOINT` (the deployment's v1 endpoint, ending in `/openai/v1/`, copied from the
      portal) and `FOUNDRY_API_KEY`.

## P0-1: Model factory

**Where:** new `backend/app/agents/models.py`; `backend/app/core/config.py`;
`backend/app/agents/study_agent.py`.

- No new dependency: `OpenAIChatModel` and `AzureProvider` come with the `openai` extra already
  installed.
- `Settings` reads `.env.local` after `.env` (later files override earlier ones); add
  `.env.local` to `.gitignore`.
- Add settings: `FOUNDRY_ENDPOINT: str | None`,
  `FOUNDRY_API_KEY: SecretStr | None`, and move `SIM_MODEL` into `Settings` (default `"demo"`; it
  is currently read from `os.environ` at import time, which `CLAUDE.md` documents as a gotcha).
  Do not name them `AZURE_OPENAI_*`: `AzureProvider` reads those from the environment on its own,
  and the app must pass its settings explicitly.
- `build_model(spec: str) -> Model | str`:

  | `spec` | Returns |
  | --- | --- |
  | `demo` | The existing deterministic scripted model |
  | `test` | `"test"` (Pydantic AI `TestModel`) |
  | `foundry:<canonical>` | `OpenAIChatModel(<default deployment>, provider=AzureProvider(azure_endpoint=..., api_key=...), profile=<pinned>)` |
  | `foundry:<canonical>@<deployment>` | Same, but the request uses `<deployment>` as the model name |
  | anything else | `ValueError` naming the allowed forms |

  `<canonical>` must be `deepseek-v3.2`; its default deployment name is `DeepSeek-V3.2`. Keep the
  allowed models in one mapping (canonical name to default deployment) so a second model, such as
  one for photo capture, is a one-line addition.
- **Pin the profile of the default deployment name.** Pydantic AI looks profiles up by
  deployment name; under a custom name, DeepSeek-V3.2 gets a generic OpenAI profile that claims
  JSON-schema output, which the model does not support. Passing
  `AzureProvider.model_profile(<canonical>)` does **not** fix this: that partial profile omits the
  JSON-output flags, and a partial profile is merged on top, so the generic values survive. Resolve
  the full profile of a model built under the default deployment name and pass it in the callable
  form, `profile=lambda _resolved: profile.copy()`, which replaces rather than merges (verified
  2026-11-11 with Pydantic AI 2.65; the first version of this spec prescribed the partial form).
- **Require a v1 endpoint** (an `https` URL whose path ends in `/openai/v1`). Only then does
  `AzureProvider` use a plain OpenAI client that ignores `OPENAI_API_VERSION`.
- Never set `temperature`, `top_p`, or `top_k` in model settings.
- **Build lazily.** Importing any `app` module must not require Foundry settings or construct a
  network client. Resolve the model when a run starts (`run_study` already accepts `model=`).
  A `foundry:` spec with missing settings raises a clear error naming both variables.
- The smoke logic lives in `app/agents/smoke.py` (testable offline with scripted models);
  `backend/scripts/smoke_foundry.py` is a thin command-line wrapper.

**Acceptance criteria:**

- [x] Unit tests (no network): `foundry:deepseek-v3.2` yields an `OpenAIChatModel` named
      `DeepSeek-V3.2` whose profile has `supports_json_schema_output == False`; the same profile
      holds for `foundry:deepseek-v3.2@my-custom-name`, whose requests use `my-custom-name`;
      `demo` and `test` behave as today; unknown specs raise `ValueError`.
- [x] A test asserts no model settings set `temperature`, `top_p`, or `top_k`.
- [x] A test asserts `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_API_KEY` in the environment are
      ignored: the provider uses the values from `Settings`.
- [x] The full suite passes with no Foundry variables set.
- [x] `backend/scripts/smoke_foundry.py` (manual, never in CI) runs one tiny study through
      DeepSeek-V3.2 with tracing on and prints the trace ID and token usage. It first checks that
      the model returns a real tool call, not tool-call text in a message (a reported Foundry
      issue), and fails clearly if not. *First live run by the owner on 2026-11-11: tool calls
      structured, study succeeded, trace visible in Phoenix.*

## P0-2: Measure tokens

- [x] With `docker compose up -d phoenix` and `PHOENIX_COLLECTOR_ENDPOINT` set, run the smoke
      script.
- [ ] Record input, output, and cache tokens per run, plus the run's request count, in
      `design.md`'s budget section; replace the assumed 7,500 / 600 figures. Add a change-log row.
- [ ] Derive the prices: run the smoke script a few times (enough tokens that the cost is not
      rounded to zero), wait for Azure Cost Management to show the cost (up to a day), and divide
      the input and output meter costs by the token totals from Phoenix. Record the rates in
      `design.md` with the date and method, add the cost table, and compare it with the
      unverified estimate of about 16 USD a month.

## P0-3: Core tables

**Where:** `backend/app/models.py` (follow the `Study` patterns: UUID primary keys,
timezone-aware timestamps, `Literal` types for closed sets, cascading deletes from `user`), one
Alembic revision. Money is **integer cents** (reviewed by the owner).

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

**Implementation notes** (decided while building, 2026-11-11; not in the table above):

- Table models skip Pydantic validation, so every closed set, calorie level, percentage, and
  `MM-DD` date is also a Postgres CHECK constraint.
- `data_snapshot` rows are protected by a trigger that rejects `UPDATE` and `DELETE`.
- `catalog_item`'s unique key uses `NULLS NOT DISTINCT`; otherwise national rows (both owner
  columns `NULL`) could repeat a slug. A CHECK ties `scope` to the owner column it requires.
- `price_observation`: a row has no household exactly when `captured_via = 'public_dataset'`.
  Deleting a vendor keeps its prices (`vendor_id` set to `NULL`).
- Produce facts on `catalog_item` (yield factor, cup weight, storage days) are nullable, because
  supplies have none. `vendor.region_id` is nullable (an online supplier has no region).
- The region row is seeded by `init_db` (run by `prestart.sh`), which inserts it only if missing.
- Test Backend runs every migration down to an empty database and back up, then `alembic check`.

**Acceptance criteria:**

- [x] Migration upgrades and downgrades cleanly (round trip; now a CI step).
- [x] Deleting a user deletes their household, members, vendors, household-scoped catalog items,
      and prices (test).
- [x] Invalid calorie levels and percentages are rejected (test).
- [x] The North Alabama region row exists after `prestart.sh`.

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

**Implementation notes** (decided while building, 2026-11-11):

- The crosswalk creates all 44 national catalog items (owner's choice); loaders then fill what
  their source has. Items with no ERS form get a `conversion_source` saying why.
- The crosswalk also names each item's FoodKeeper storage location (`refrigerator` or `pantry`),
  so the loader never guesses which storage time applies. Storage time is the range minimum,
  preferring FoodKeeper's "from date of purchase" columns.
- `catalog_item.storage_snapshot_id` (new) records the FoodKeeper snapshot; `snapshot_id` stays the
  conversion (ERS) snapshot.
- The P0-3 constraint capping `preparation_yield_factor` at 1 was wrong (ERS: dried beans 2.47,
  collard greens 1.16); the same migration relaxes it to `> 0`.
- ERS prices load as one public observation per item: `price_cents` rounds the price per pound,
  so `raw_text` keeps the full-precision value. `observed_on` is the middle of the data year.
- The FDC loader reads the zip as downloaded and detects Foundation or SR Legacy from its files.
  The 2026-10-29 Foundation release is incomplete, so 2026-04-30 is used.
- Raw ERS and FoodKeeper files are committed under `data/sources/`, byte for byte; FDC zips are
  not (about 10 MB), and their URLs and hashes are in the seed-data README.
- Each file is loaded in its own transaction inside a Prefect flow (`python -m app.seed`).
- Added in P0-5: each snapshot records a fingerprint of the crosswalk rows used
  (`crosswalk_sha256`) and its `loaded_at` time. Loading a file again through a changed
  crosswalk is a new snapshot that applies the new mapping; the latest snapshot of a source is the
  current one. Snapshots loaded before this have no fingerprint and stay as history.

**Acceptance criteria:**

- [x] Each loader run twice on the same file creates one snapshot and no duplicate rows.
- [x] The ERS loader's computed price per cup matches ERS's published column within 0.001 USD for
      every row, **except two pairs where ERS's published column is swapped** (Artichoke Fresh
      and Canned, Lima beans Frozen and Dried); the test checks those pairs swap exactly (see
      known answers).
- [x] Every catalog item has a `conversion_source`; stand-ins point at the item they borrow from.
- [x] A report command lists first-crop-set items missing a price, conversion, nutrients, or
      storage time, matching the gaps in `design.md` ("ERS coverage"), plus tomato's storage
      (FoodKeeper gives no number) and garden peas (now in the table).
- [x] Loader tests use fixtures only; no network in CI.

## P0-5: Close out

- [x] Tick the Phase 0 tasks in `roadmap.md`; update `design.md` and its change log.
- [x] `TESTING.md`: new test files and a change-log row with CI timings.
- [x] Write `docs/specs/phase-1.md` for owner review.

## Gate

Migrations round-trip; seed loads are repeatable and versioned; CI is green with scripted models
and no Foundry settings; one live Foundry run appears in Phoenix with token counts, and
`design.md`'s budget section uses measured numbers.

**Status (2026-11-11):** met except the last clause. Migrations round-trip in CI; loading twice is
a no-op (tests, and the real data on the dev database); CI is green with no Foundry settings; the
owner's smoke run is in Phoenix with token counts. The budget section still uses assumptions: the
smoke run's tokens came from the old study agent, and the Azure cost data is too small to derive
prices. Both move to Phase 1 (P1-9), where the nightly evals produce enough real usage.

## Decisions for the owner's review

| Decision | Default in this spec | Outcome |
| --- | --- | --- |
| Money representation | Integer cents | Implemented in P0-3 (PR #12, merged by the owner) |
| `SIM_MODEL` default | `demo` (live models only when set explicitly) | Implemented in P0-1 (PR #11) |
| National data rows | `region_id` nullable rather than a "national" region row | Implemented in P0-3 |
| Crosswalk review | The owner approves `catalog_crosswalk.csv` before loaders depend on it | Approved 2026-11-11 |
