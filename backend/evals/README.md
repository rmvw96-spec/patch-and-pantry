# Golden evals

Spec P1-9. Typed price lists go through the real capture code (`capture_typed_list`) with a live
model, and each line is scored against what should happen to it. The cases cover the problems
found while dogfooding, and they watch for the model getting worse.

## Running

From `backend/`, with the catalog loaded (`uv run python -m app.seed all`). A live model also
needs `FOUNDRY_ENDPOINT` and `FOUNDRY_API_KEY` in `../.env.local`.

```bash
uv run python -m evals --cases extraction                  # SIM_MODEL, both readers, 3 runs
uv run python -m evals --model foundry:deepseek-v3.2       # every case: about 5 minutes
uv run python -m evals --model demo --repeat 1             # free: the demo reader
uv run python -m evals --case count-words --reader model --repeat 1 --verbose
uv run python -m evals --json results.json                 # per-case results, for the baseline
```

- **Nothing is saved.** Each run makes its own user, household, and store inside a transaction
  that is rolled back, so it is safe on the dev database. Household aliases do not apply.
- **Both readers by default.** `model` sends every line to the model, which watches the model
  itself. `rules_first` is how the app reads lists: the rules read what they can, and the model
  reads only the lines they are not sure of.
- **Rate limits.** Foundry limits tokens a minute. The runner runs 2 cases at once
  (`--concurrency`) and waits and retries on HTTP 429. Without the retry, a rate limit in a
  rules-first run would look like a model mistake, because capture turns the model's lines into
  "unreadable" questions.
- **Exit status** is 1 when any case fails, and 2 when no case matches.

## Cases

Cases are in `cases/*.yaml`. The expected answers need the owner's review.

| File | Group | What it holds |
| --- | --- | --- |
| `1-owner-lists.yaml` | extraction | The owner's four real lists, whole (ALDI, Family Dollar, Publix, the Shoals market) |
| `2-challenges.yaml` | extraction | Short lists for each dogfooding problem: the unit in the price, a price for one times a count, N for $M, "ct" and "pk", a weight bought, misspellings, descriptions around the item, canned and frozen items, per-piece items and nuts |
| `3-clarification.yaml` | clarification | Lines that must become a question: ambiguous words, items not in the catalog, a box or basket, no price |
| `4-behavior.yaml` | behavior | Notes between prices (store names, dates, totals), the dieting question (P1-8), and text that gives the reader orders |

**How a case passes (D2).** An extraction case needs at least 95% of its fields right in every
run. A clarification or behavior case needs every field right in every run.

**What is scored.** Each line ends as one outcome:
- a **price**: a draft, or a question about one piece's weight or size, which carries the same
  draft. CI loads no piece weights, and a seeded dev database does.
- a **question** of a given kind;
- a **note**, set aside.

The outcome is scored first. For a price, the item, quantity, unit, and total price are scored
too; for a question, the items it offers. Only the fields a case gives are scored. Sizes are not
scored, because a size is kept only when piece weights are loaded. Questions about a price or
unit do not carry the matched item, so a case can name only the question's kind.

```yaml
- text: "Limes, lemons $1 each"
  why: Two items at one price; either one, or a question.
  expect:
    - {item: Lime, quantity: 1, unit: each, price: 100}
    - {item: Lemon, quantity: 1, unit: each, price: 100}
    - {ask: which_item, options: [Lime, Lemon]}
```

Several `expect` entries mean any of them is fine, and the closest one is scored. `price` is in
cents, for the whole amount bought. `ask` is a question kind: `which_item`, `no_match`,
`missing_price`, `missing_unit`, `unknown_unit`, or `unreadable`.

`scripts/compare_readers.py` still scores the reading alone (no catalog) against
`evals/lists/*.csv`.
