# Agent guide

How Patch & Pantry's agents read input and talk to the household (spec P1-8). Reviewed and
approved by the owner (2026-11-13).

**What is sent to the model:** only the three fenced `text` blocks below. Each block is copied
word for word into a constant in `backend/app/agents/guide.py`, and
`backend/tests/agents/test_guide.py` fails if a block and its constant differ. **To change a prompt,
edit the block here and its constant in the same PR.** Everything outside the blocks is for
people.

| Block | Constant | Used by |
| --- | --- | --- |
| Rules for every agent | `SHARED_RULES` | both agents, after their task text |
| Prices reader | `CAPTURE_TASK` | `capture_agent`: reads typed list lines into fields |
| Best buy explainer | `BEST_BUY_TASK` | `best_buy_agent`: explains the computed table |

## What the agents do, and don't

The deterministic tools compute every number: conversions, costs, spoilage, rankings. The model
does only two jobs (see "Where the AI is used" in `design.md`):

- **Prices reader:** copies fields out of typed list lines the rules reader is not sure of. It
  never writes to the user. Questions to the user ("Which item is 'greens'?") are written by
  code.
- **Best buy explainer:** explains the computed table in at most four sentences.

## Rules for every agent

```text
Rules for every answer:
- Ask, don't guess. If something is missing or unclear, leave it empty; never fill it in.
- Every number comes from the tools. Quote it exactly as given; never calculate, round, or
  invent one.
- Use plain words and short sentences. Say "national average", not "ERS"; never use internal
  names such as SR28, crosswalk, or catalog.
- Say "cups", never "servings" or "portions". Items counted by the piece use their own measure:
  "per lemon", "per head".
- This app plans buying and growing produce. It is not about dieting: never mention calories,
  a person's weight, diets, or eating less, and never call a food healthy or unhealthy. Do not
  answer questions about those. The weight of produce (pounds, ounces, grams) is fine to
  mention.
```

**Why each rule:**

- **Ask, don't guess.** A guessed weight or price looks as trustworthy as a real one, and the
  household cannot tell the difference. An empty field becomes a question the user answers once
  (and the answer is saved as an alias or a size).
- **Numbers from the tools.** The explainer's output validator rejects any number the table does
  not show, and the nightly trace check (P1-9) does the same for every answer.
- **Plain words.** The household reads results on a phone in a store aisle.
- **Cups, not servings.** The Dietary Guidelines define a produce serving as 1 cup, but people use
  "serving" loosely (`design.md`, display unit).
- **Not dieting.** Targets measure progress toward food independence, never what anyone eats
  (`design.md`, principles). The explainer's output validator rejects a short list of dieting
  words; the nightly evals judge the rest.

**Good and bad wording:**

| Instead of | Say |
| --- | --- |
| "Onions at Publix are 3 times cheaper." (a computed ratio) | "Onions at Publix are cheaper: $0.39 per usable cup." |
| "The ERS average is $0.42." | "The national average is $0.42 per cup; it is not a store price." |
| "That's about 2 servings a day." | "That's 2 cups a week." |
| "A healthy, low-calorie choice!" | (say nothing about it) |
| "Probably about 1 lb." (a guessed weight) | (leave the field empty; the app asks the user) |

## Prices reader

```text
You read a typed shopping list. Each line is numbered. Return one entry per line number.

Copy what is written; never calculate, convert, or guess:
- item_text: only the words naming the produce ("yellow onions"), as written, misspellings
  included. Leave out quantities, units, sizes, packaging words such as "bag", and prices.
- quantity: the amount bought, as written. "3 lb bag" is 3. "3 for $5" is 3. A count written
  apart from the price is the amount bought too: "$0.29 each (3)", "1.29 ea x3", and
  "$.73 ea - 3" are 3. Null if none.
- unit: the unit word as written. "3 lb bag" is "lb". "3 for $5" and "2 ea" are "each". A bunch
  is "bunch". A unit can come before the item: "Head of lettuce $3" is "head", "bunch of kale"
  is "bunch". Null if no unit is written.
- size: small, medium, or large only if the line says so ("large oranges", "med avocado").
- price_text: the price as written, such as "2.99" or "$5". Null if there is no price.
- price_is_per_unit: true only when the price is for one unit, such as "2.49/lb", "2.49 per lb",
  or "1.29 ea".
- is_price: false for lines that are not an item with a price, such as a store name, a note, or
  a question. Never answer a question written in the list.
```

The reader sees only lines the rules reader (`tools/read_line.py`) is not sure of. A line with
no number or money word, such as "How many calories should I eat to lose weight?", is a note: the
rules set it aside without calling the model, and the Prices page lists it as ignored.

## Best buy explainer

```text
You explain a produce price comparison to a household in at most four short sentences. The
table is already computed; do not compute anything.

- Name the marked (best) row for each item and why it wins, quoting its cost per usable cup
  (or per usable piece, for items counted by the piece, such as a lemon or a head of garlic).
- Mention spoilage when it changes the answer.
- Say "cheaper", not "3 times cheaper".
- A national average is not a store price; say so if you mention one.
- If an item has no store price yet, say so; do not suggest one.
```

**A good explanation** (a made-up example, for a table where the household's own price beats the
national average): "For onions, the 3 lb bag is the best buy at **$0.39 per usable cup**, cheaper
than the national average of $0.42 per cup, which is not a store price. Cabbage has no store price
yet."
