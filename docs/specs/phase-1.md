# Phase 1 spec: Store-only best buy

Status: **reviewed** (2026-11-11; decisions D1 to D7 settled). Roadmap: [`../roadmap.md`](../roadmap.md).
Design: [`../design.md`](../design.md). Known answers: [`known-answers.md`](known-answers.md).
The owner's decisions are collected at the end and recorded in `design.md`.

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
      can be committed (no names or addresses). Needed by P1-9; one list by P1-5 helps set the
      alias list and the fuzzy-match cutoff.
- [x] **GitHub Actions secrets** `FOUNDRY_ENDPOINT` and `FOUNDRY_API_KEY` for the nightly eval
      workflow (repository settings; never in a file).
- [x] Answers to the decisions at the end of this spec (all decided 2026-11-11).

## P1-2: Household setup

**Where:** `backend/app/api/routes/household.py`. The settings page moved to P1-7 (owner,
2026-11-11: no frontend changes until the agents and tools exist).

- `GET /household` creates the user's household on first access (region `north-al`,
  `user_setting=store_only`, one member at 2,000 calories). `PUT /household` replaces
  `produce_budget_cents_per_week` (null for none; at most 1,000,000 cents, so a typo cannot
  overflow the column) and the members: a list of 1 to 20 calorie levels, nothing else.
  Unknown fields are refused with a 422 (`extra_forbidden`) rather than dropped, so a client
  cannot send a name or age and believe it was stored.

**Acceptance criteria:**

- [x] Members can be added and removed; each is only a calorie level.
- [x] Deleting the account deletes the household (existing cascade test extended).

## P1-3: Count units

**Where:** new table `catalog_portion`; `app/seed/loaders.py` (`load_sr28_portions`);
`app/seed_data/catalog_portions.csv`.

- `catalog_portion`: `id`, `catalog_item_id` (cascade), `unit` (Literal: `each`, `bunch`,
  `head`, `ear`, `pint`), `size` (nullable: `small`, `medium`, `large`), `grams` (of the whole
  purchased unit), `edible_grams` and `refuse_pct` (for loaded rows), `description` (the
  source's wording), `source` (text), `snapshot_id` (nullable), `measured_on` (nullable). Every
  portion has a snapshot or a measured date (CHECK).
- **Household measures are edible weight.** SR28's documentation: "Weights are given for edible
  material without refuse." So `grams = edible_grams / (1 - refuse_pct / 100)`, with refuse from
  SR28's food descriptions, and `normalize_price` then applies the preparation yield to `grams`
  as it does to any purchased weight. Without this step the refuse would be removed twice.
  Checks against SR28's own hints: large green pepper 164 / 0.82 = 200 g ("2-1/4 per lb" =
  202 g); peeled eggplant 458 / 0.81 = 565 g ("yield from 1-1/4 lb" = 567 g).
- Loaded from the **SR28 ASCII release** (`WEIGHT.txt` and `FOOD_DES.txt`): FDC's SR Legacy CSV
  has the same weights (all 43 mapped rows match) but not the refuse. A separate owner-reviewed
  file, `catalog_portions.csv`, names the SR28 weight for each item, unit, and size (it outgrew a
  crosswalk column). Weights that do not fit SR28's refuse definition are left out and say why
  in the notes: spinach's 10 oz package (no refuse to remove) and the unpeeled eggplant.
- Price observations gain `unit_grams` and `weight_source` (`weighed` or `estimated`), set
  together or not at all (CHECK).
- Not covered on purpose: carrots, radishes, and okra (sold by weight or bunch), and pints (no
  SR28 weight fits). Butternut squash is sold by count but SR28 has no whole-squash weight; the
  coverage report lists it.
- FDC has **no bunch weights** (greens are given as chopped cups). A bunch is asked about at
  capture (D3): the question suggests weighing the bunch if the user can, and otherwise giving an
  estimate. The answer goes on that price observation, with `weight_source` (`weighed` or
  `estimated`; new column, CHECK constraint), and the table labels estimated rows. No bunch
  portions are measured in advance; a household that keeps weighing the same bunch can later save
  it as a portion (household scope).

**Acceptance criteria:**

- [x] Every first-crop-set item sold by count has a portion (`each`, `head`, `ear`, or `bunch`),
      or appears in the coverage report as missing one (butternut squash).
- [x] Owner-measured portions record their source and date (`ck_catalog_portion_provenance`).
- [x] Owner review of `catalog_portions.csv` (2026-11-11): approved, with potato refuse set to
      10% instead of SR28's 25% (`refuse_pct_override`; the source text records both).

## P1-4: Tool `normalize_price`

**Where:** `backend/app/tools/normalize.py` (pure functions; no database access inside the math).

```python
def normalize_price(
    price_cents: int, quantity: float, unit: str, item: CatalogFacts, portion_grams: float | None
) -> Normalized  # grams_purchased, grams_edible, cups, cost_per_cup_cents (float)
```

- Units: `lb`, `oz`, `kg`, `g` by weight; `each`, `bunch`, `head`, `ear`, `pint` through a portion
  (`grams_purchased = quantity x portion.grams`, purchased weight; P1-3) or a capture weight
  (`unit_grams`).
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
- Tool `find_catalog_item(text)` (D7): deterministic matching in three steps, so users never
  need to know catalog names, and photo and receipt capture (Phase 2) can reuse it.
  1. Exact match (case and plural folded) against catalog names, the reviewed alias list
     (`catalog_aliases.csv`: "russets", "yellow onions", "collard greens"...), and the
     household's confirmed aliases.
  2. Fuzzy match against the same names, with Python's standard `difflib` (no new dependency;
     revisit if the evals show misses). Returns up to 5 candidates with scores.
  3. One candidate above the cutoff and clearly ahead of the next becomes the draft's item,
     shown as "matched 'yellow onoins' to Onion"; anything else becomes a question listing the
     candidates ("greens": collards, turnip greens, mustard greens...).
  Every match is visible on the draft, and nothing is saved until the user confirms. A confirmed
  fuzzy or chosen match is saved as a household alias (`household_alias`: household, text,
  catalog item), so the same text matches exactly next time. Spelling cannot map "russets" to
  Potato; the alias list covers names like that. The model chooses nothing: it passes the tool's
  result through. The cutoff is set from the owner's real lists and checked by the evals.
- Questions are required when a unit has no portion ("kale bunch 2.50"), an item has no clear match, or
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
- Prices (D5): the household's own prices from the last 30 days come first. An item with none
  falls back to the ERS average, which becomes its priced row, labeled "national average (ERS
  2023), not a store price"; the shopping list then shows it under "no store price yet" rather
  than a vendor. The ERS average also appears on every item as a labeled reference row, for
  comparison with the household's prices. An item with neither (for example mustard greens) has
  a "no price" row.
- **Spoilage** (design: "storage life and how fast the household uses the item"): usable cups =
  min(cups bought, cups used per day x storage days). Storage days are the **midpoint** of the
  item's FoodKeeper range (D4). The loader stores the range maximum next to the minimum
  (`catalog_item.storage_days_max`, a migration and a new FoodKeeper snapshot).
- **Cook and freeze extras** (D4): a per-item household setting (`household_item_setting`:
  household, catalog item, `freeze_extras`). When it is on, the household cooks the item on
  purchase and freezes what it will not eat within a few days, so storage days are the freezer
  midpoint of FoodKeeper's "Leftovers, without meat" entry (ID 174, "such as cooked vegetables":
  1 to 2 months, 45 days). The loader records entry 174 from the same FoodKeeper snapshot. The
  row in the table carries a note: "assumes you freeze what you won't eat in 3-4 days" (entry
  174's refrigerator range).

**Worked example of the spoilage rule** (range midpoints; collards and kale at ERS 2023 prices):

| Purchase | Use | Setting | Keeps | Usable cups | Spoiled | Cost per usable cup |
| --- | --- | --- | --- | --- | --- | --- |
| Onions, 3 lb, 2.99 | 2 cups/week | - | 30 days (1 month) | 7.66 of 7.66 | 0% | 0.39 |
| Collards, 1 lb, 2.48 | 3 cups/week | - | 2.5 days (1-4) | 1.07 of 4.05 | 74% | 2.31 |
| Collards, 1 lb, 2.48 | 3 cups/week | freeze extras | 45 days (1-2 months) | 4.05 of 4.05 | 0% | 0.61 |
| Kale, 1 lb, 3.57 | 3 cups/week | - | 4 days (3-5) | 1.71 of 3.66 | 53% | 2.08 |
| Kale, 1 lb, 3.57 | 3 cups/week | freeze extras | 45 days (1-2 months) | 3.66 of 3.66 | 0% | 0.97 |

Why not other rules (worked through with the owner, 2026-11-11): FoodKeeper minimums gave
collards 89% spoilage (5.79 per usable cup) and turnip greens 82% (6.93); the range maximum still
left collards at 58% (1.45). Cooking on purchase without freezing barely helps, because cooked
leftovers keep 3 to 4 days, about as long as raw greens (collards 1.65, kale 2.38, spinach 3.00).
Adding raw and cooked days together was rejected: FoodKeeper does not say cooking resets the
clock, so the numbers would rest on an unsourced food-safety claim. FoodKeeper has no
cooked-greens entries; entry 174 is its general one for cooked vegetables.

**Acceptance criteria:**

- [ ] Known-answer tests for the table, the marked row, and spoilage, including an empty price
      book and an item with no conversion.
- [ ] The agent's explanation quotes only numbers present in the table (trace check).

## P1-1: Grocery analysis runs replace studies

**Order:** done after P1-6 (owner, 2026-11-11). The ID is kept so references stay valid. Doing
the swap once the capture agent and `compare_best_buy` exist means the runs, the `demo` model
scripts, and the replacement for the Studies page (with its Playwright test) are built against
the real agents instead of placeholders that would need rework. Until then the Studies feature and
its E2E test stay as they are, and P1-2 to P1-6 are tested directly (scripted models, no runs or
pages).

**Where:** `backend/app/models.py`, `backend/app/api/routes/grocery_analysis_runs.py`,
`backend/app/grocery_analysis_jobs.py`, `backend/app/agents/`, one Alembic revision; frontend
`components/GroceryAnalysis/` only if a page needs it.

- New table `grocery_analysis_run` (D1): `id`, `owner_id` (cascade), `kind` (Literal: `price_capture`,
  `best_buy`), `input` (text), `status` (`queued`, `running`, `succeeded`, `failed`), `result`
  (JSON), `tools_used` (JSON list of tool names in call order), `data_snapshot_ids` (JSON list:
  every snapshot a tool read), `model` (the `SIM_MODEL` spec used), `input_tokens`,
  `output_tokens`, `requests`, `error_reason`, `error_message`, `created_at`, `started_at`,
  `finished_at`.
- `POST /grocery-analysis-runs` (202, Redis idempotency key as for studies) and
  `GET /grocery-analysis-runs/{id}`; users read only their own runs. Every exit path leaves a
  run `succeeded` or `failed` and sets `grocery_analysis_run.status` on the
  `grocery_analysis_run` span (the rule `CLAUDE.md` states for studies).
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

## P1-7: Pages and shopping list

- **Household settings** (moved from P1-2): the weekly produce budget and the number of people,
  with an optional calorie level per person (the 12 Dietary Guidelines levels in a select). No
  names, ages, or body data.
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

## Later in Phase 1: explore smaller purchases

Not a task yet; explore after P1-6 works. The spoilage examples show the purchase size is often
the problem: 1 lb of collards is about 4 cups, more than a week's use at 3 cups a week, so any rule
that respects storage life spoils the rest. `compare_best_buy` could suggest a smaller purchase
("buy 1/2 lb") when the household cannot use a whole unit before it spoils. Questions to settle
first: which purchase sizes a store or market actually sells (bunches, bags, loose by weight), and
whether the suggestion belongs in the table or the explanation. Write it up as a task, or park it
in `design.md`, after the owner reviews the idea.

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
| D1 | Name of the job record | **Decided:** `grocery_analysis_run` |
| D2 | Eval pass threshold | **Decided:** exact answers 3 of 3 runs; extraction at least 95% of fields. Start here and lower it only if DeepSeek-V3.2's measured results justify it |
| D3 | A bunch with no measured weight | **Decided:** the agent asks, suggesting the user weigh it or estimate; estimates are labeled. No bunch weights measured in advance. See P1-3 |
| D4 | Storage days for spoilage | **Decided 2026-11-11:** FoodKeeper range **midpoint**, plus a per-item "cook and freeze extras" household setting that uses the cooked-leftovers freezer midpoint (45 days). See P1-6 |
| D5 | Which prices count | **Decided:** the household's own from the last 30 days first; otherwise the ERS average, labeled; ERS always shown as a reference row. See P1-6 |
| D6 | Retiring Studies | **Decided:** delete the code (git history keeps it) once the `grocery_analysis_run` equivalents of its tests pass |
| D7 | Matching typed item names | **Decided:** exact match on names and aliases, then fuzzy candidates; a clear match is shown on the draft, anything else is a question; confirmed matches become household aliases. See P1-5 |
