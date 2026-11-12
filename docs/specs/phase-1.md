# Phase 1 spec: Store-only best buy

Status: **draft for owner review** (2026-11-11). Roadmap: [`../roadmap.md`](../roadmap.md).
Design: [`../design.md`](../design.md). Known answers: [`known-answers.md`](known-answers.md).
Decisions with proposed defaults are collected at the end; the owner's answers go into
`design.md`.

## Goal

The first useful release: type in prices from any store and get the cheapest way to buy this
week's produce, per usable cup, with a printable shopping list. It also replaces the Studies
feature with a general agent **run**, so the pipeline simulator leaves the codebase.

**Out of scope:** local sources and farm shares (Phase 2), photos and receipts (Phase 2, needs a
vision model), growing (Phase 3), independence progress (Phase 4), presets other than Lowest cost
(Balanced and Most homegrown need local and garden options), AMS sale prices (Phase 2).

## Prerequisites (owner)

- [ ] **Real typed lists** for golden evals: at least 3 lists (about 30 lines) from real trips to
      at least 2 stores, as typed on a phone, with the expected item, quantity, unit, and price
      for each line. These become the extraction cases; they are the owner's own data, so they
      can be committed (no names or addresses).
- [ ] **GitHub Actions secrets** `FOUNDRY_ENDPOINT` and `FOUNDRY_API_KEY` for the nightly eval
      workflow (repository settings; never in a file).
- [ ] Answers to the decisions at the end of this spec, especially **spoilage** (D4).
- [ ] Hours per week available for building (roadmap, "Decisions to settle").

## P1-1: Agent runs replace studies

**Where:** `backend/app/models.py`, `backend/app/api/routes/runs.py`, `backend/app/run_jobs.py`,
`backend/app/agents/`, one Alembic revision; frontend `components/Runs/` only if a page needs it.

- New table `agent_run`: `id`, `owner_id` (cascade), `kind` (Literal: `price_capture`,
  `best_buy`), `input` (text), `status` (`queued`, `running`, `succeeded`, `failed`), `result`
  (JSON), `tools_used` (JSON list of tool names in call order), `data_snapshot_ids` (JSON list:
  every snapshot a tool read), `model` (the `SIM_MODEL` spec used), `input_tokens`,
  `output_tokens`, `requests`, `error_reason`, `error_message`, `created_at`, `started_at`,
  `finished_at`.
- `POST /runs` (202, Redis idempotency key as for studies) and `GET /runs/{id}`; users read only
  their own runs. Every exit path leaves a run `succeeded` or `failed` and sets `run.status` on
  the `run` span (the rule `CLAUDE.md` states for studies).
- Retire the Studies feature in the same pull request: `study` table (migration drops it),
  routes, `study_jobs.py`, `study_agent.py`, `simulator.py`, `demo_model.py`'s study script, the
  Studies page, and their tests. Their patterns move to the run equivalents first, so coverage
  of idempotency, terminal states, tracing, and the live-call safety switch is kept, not lost.
- The `demo` model spec scripts the new agents (deterministic capture and best-buy answers), so
  Playwright keeps running without a model.

**Acceptance criteria:**

- [ ] The run API tests cover what the studies tests covered: idempotency per user, terminal
      states on every exit path, users reading only their own runs, tracing attributes.
- [ ] A run records the snapshots and the model it used.
- [ ] No reference to the simulator or studies remains (`git grep -i "study\|simulat"` finds only
      history in docs).

## P1-2: Household setup

**Where:** `backend/app/api/routes/household.py`; settings page section.

- `GET /household` creates the user's household on first access (region `north-al`,
  `user_setting=store_only`). `PUT /household` sets `produce_budget_cents_per_week` and the
  members: a list of calorie levels (default 2,000), nothing else.
- The page asks for the weekly produce budget and the number of people, with an optional calorie
  level per person (the 12 Dietary Guidelines levels in a select). No names, ages, or body data.

**Acceptance criteria:**

- [ ] Members can be added and removed; each is only a calorie level.
- [ ] Deleting the account deletes the household (existing cascade test extended).

## P1-3: Count units

**Where:** new table `catalog_portion`; `app/seed/loaders.py`.

- `catalog_portion`: `id`, `catalog_item_id` (cascade), `unit` (Literal: `each`, `bunch`,
  `head`, `ear`, `pint`), `size` (nullable: `small`, `medium`, `large`), `grams` (of the whole
  purchased unit), `source` (text), `snapshot_id` (nullable).
- Loaded from FoodData Central SR Legacy household measures (`food_portion.csv`) for the first
  crop set, through a crosswalk column naming the portion rows to use. Example: tomato, 1 medium
  whole (2-3/5" dia) = 123 g; okra, 8 pods (3" long) = 95 g.
- FDC has **no bunch weights** (greens are given as chopped cups). A bunch therefore needs either
  a weight the owner measures (`source = "owner measurement"`) or a question to the user (D3).

**Acceptance criteria:**

- [ ] Every first-crop-set item sold by count has an `each` portion, or appears in the coverage
      report as missing one.
- [ ] Owner-measured portions record their source and date.

## P1-4: Tool `normalize_price`

**Where:** `backend/app/tools/normalize.py` (pure functions; no database access inside the math).

```python
def normalize_price(
    price_cents: int, quantity: float, unit: str, item: CatalogFacts, portion_grams: float | None
) -> Normalized  # grams_purchased, grams_edible, cups, cost_per_cup_cents (float)
```

- Units: `lb`, `oz`, `kg`, `g` by weight; `each`, `bunch`, `head`, `ear`, `pint` through a portion.
  Anything else raises `UnknownUnit`; a count unit without a portion raises `NeedsPortion` (the
  agent turns it into a question).
- `grams_edible = grams_purchased x preparation_yield_factor`; `cups = grams_edible /
  (cup_equivalent_lb x 453.59237)`. Stand-ins use the borrowed item's factors and say so.
- Items with no conversion (for example eggplant) raise `NoConversion`; the comparison shows them
  without a per-cup figure rather than guessing.

**Worked examples** (from the loaded ERS data; to be added to `known-answers.md` when accepted):

| Input | Edible grams | Cups | Cost per cup |
| --- | --- | --- | --- |
| Onions, 3 lb bag, 2.99 (yield 0.9, cup 0.3527 lb) | 1,224.7 | 7.6552 | 0.3906 |
| Potatoes, 5 lb bag, 3.49 (yield 0.8113, cup 0.2646 lb) | 1,840.0 | 15.3307 | 0.2276 |
| Collards, 1 lb, 2.48 (yield 1.16, cup 0.2866 lb) | 526.2 | 4.0475 | 0.6127 |

**Acceptance criteria:**

- [ ] The ERS known answers and these examples match within 0.0001.
- [ ] Unknown units, missing portions, and missing conversions each raise their own error.

## P1-5: Typed-list capture

**Where:** `backend/app/agents/capture_agent.py`; tools in `backend/app/tools/`.

- Input: a typed list (one item per line) plus the vendor and date chosen in the form. Output: a
  list of draft price observations and a list of questions. Nothing is saved until the user
  confirms the drafts on the page.
- The agent extracts item text, quantity, unit, and price exactly as written ("3 for $5" is
  quantity 3, unit `each`, 500 cents for the lot). It does **no arithmetic**: per-pound or
  per-cup figures come only from `normalize_price`.
- Tool `find_catalog_item(text)`: exact match against catalog names and a reviewed alias list
  (`catalog_aliases.csv`: "yellow onions", "russets", "greens"...). No fuzzy matching; no match
  becomes a question, not a guess.
- Questions are required when a unit has no portion ("kale bunch 2.50"), an item has no match, or
  the price or quantity is unreadable. Never a silent guess (`known-answers.md`, behavioral cases).

**Acceptance criteria:**

- [ ] Scripted-model tests: each behavioral case in `known-answers.md` produces the expected
      drafts or questions.
- [ ] A draft keeps `raw_text` exactly as typed; nothing is saved before confirmation.

## P1-6: Tool `compare_best_buy`

**Where:** `backend/app/tools/best_buy.py`.

- Input: the shopping need (catalog items with cups per week), the household's price book, and
  the date. Output: the trade-off table: one row per (item, vendor, price observation) with price,
  cups bought, usable cups, spoilage %, cost per usable cup, and the source of each factor; the
  lowest cost per usable cup per item is marked (the Lowest cost preset).
- Prices count when they are the household's own and observed within the last 30 days (D5).
  ERS averages appear as a separate, labeled reference row: a national average, not a store.
- **Spoilage** (design: "storage life and how fast the household uses the item"): usable cups =
  min(cups bought, cups used per day x storage days), with storage days from FoodKeeper (D4).

**Worked example of the spoilage rule** (FoodKeeper minimums, as `design.md` specifies today):

| Purchase | Use | Keeps | Usable cups | Spoiled | Cost per usable cup |
| --- | --- | --- | --- | --- | --- |
| Onions, 3 lb, 2.99 | 2 cups/week | 30 days | 7.66 of 7.66 | 0% | 0.39 |
| Collards, 1 lb, 2.48 | 3 cups/week | 1 day | 0.43 of 4.05 | 89% | 5.79 |
| Kale, 1 lb, 2.99 | 3 cups/week | 3 days | 1.29 of 3.66 | 65% | 2.33 |

FoodKeeper's minimum for "Greens" is 1 day (range 1 to 4), so the rule makes leafy greens look
nine times more expensive than their shelf price per cup. This is the most consequential choice
in Phase 1; see D4.

**Acceptance criteria:**

- [ ] Known-answer tests for the table, the marked row, and spoilage, including an empty price
      book and an item with no conversion.
- [ ] The agent's explanation quotes only numbers present in the table (trace check).

## P1-7: Pages and shopping list

- **Price capture:** vendor, date, and a text box; shows drafts and questions; confirm saves.
- **Price book:** the household's prices (item, vendor, date, price as written, cost per cup),
  with edit and delete.
- **Best buy:** choose items and cups per week; shows the trade-off table and a short
  explanation from the agent.
- **Shopping list:** the marked rows grouped by vendor, with quantities as the store sells them;
  a print layout (useful offline, per the design's principles).
- One Playwright path per page, in demo mode.

## P1-8: Agent instructions and tone guide

**Where:** `docs/agent-guide.md` (owner reviews), loaded into the agents' instructions.

- Ask, don't guess; quote tool numbers only; plain words; "cups", never "servings"; no dieting
  language (the behavioral case "How many calories should I eat to lose weight?").

## P1-9: Golden evals and the nightly run

**Where:** `backend/evals/` (cases as YAML; a runner built on `pydantic-evals`, which is not
installed yet and would be a new dependency), `.github/workflows/nightly-evals.yml`.

- Cases: extraction field by field (from the owner's real lists), clarification cases,
  conversions and rankings exact, and a trace check that every number in an answer appears in a
  tool output.
- Each case runs 3 times against DeepSeek-V3.2. Pass threshold (D2): exact-answer cases pass 3 of
  3; extraction at least 95% of fields correct.
- Nightly at 03:00 Central on `master`, using the repository secrets; never on pull requests.
  Results are compared with `backend/evals/baseline.json` (the last good run: pass rate per case,
  run time, tokens), and a drop fails the workflow. The baseline updates only by a reviewed PR.
- Token counts from these runs complete Phase 0's P0-2: measured tokens per run, prices derived
  from Azure Cost Management, and the budget table.

**Acceptance criteria:**

- [ ] `uv run python -m evals --cases extraction` runs locally against `SIM_MODEL`.
- [ ] The nightly workflow passes zizmor and pins actions by SHA, like the others.

## P1-10: iPhone home-screen icon

Replace the stopgap `apple-touch-icon.png` with a purpose-made 180 px full-bleed icon and check it
on a phone (moved here from the roadmap unchanged).

## Gate

Golden evals pass the threshold on DeepSeek-V3.2; the owner uses the app for two weeks of real
shopping; the Studies feature is gone with no loss of test coverage; `design.md`'s budget section
uses measured token counts and derived prices.

## Decisions for the owner's review

| # | Decision | Default in this spec |
| --- | --- | --- |
| D1 | Name of the job record | `agent_run` ("run") |
| D2 | Eval pass threshold | Exact answers 3 of 3 runs; extraction at least 95% of fields |
| D3 | A bunch with no measured weight | The agent asks; the owner may record measured bunch weights as catalog portions |
| D4 | Storage days for spoilage | FoodKeeper **minimum**, as `design.md` says, which gives greens 89% spoilage at weekly shopping (table above). Alternatives: the range midpoint (greens 2.5 days), or the maximum (4 days) |
| D5 | Which prices count | The household's own, from the last 30 days; ERS averages shown as a labeled reference row only |
| D6 | Retiring Studies | Delete the code (git history keeps it), after the run equivalents of its tests pass |
| D7 | Matching typed item names | Catalog names plus a reviewed alias list; no fuzzy matching |
