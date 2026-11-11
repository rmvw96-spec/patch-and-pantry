# Seed data

Reference data the app loads into versioned snapshots (see `docs/design.md`, "Data sources,
caching, and versioning"). Never edit a file here in place without recording why in the
`docs/design.md` change log; loaders store each new file version as a new snapshot.

| File | What it holds | Source | Retrieved | Notes |
| --- | --- | --- | --- | --- |
| `north_alabama_planting_calendar.csv` | 63 spring and fall planting windows for 39 crops: published central-Alabama dates, derived North Alabama dates, days to maturity, spacing | Alabama Cooperative Extension, "Alabama Vegetable Garden Planting Chart" ([planting guide](https://www.aces.edu/blog/topics/lawn-garden/planting-guide-for-home-gardening-in-alabama/)); PDF in `data/sources/alabama-extension/` | 2026-11-11 | North Alabama dates are derived (spring +10 days, fall -10 days) and **unverified**; do not use them as golden answers. Rebuild with `uv run scripts/data/build_planting_calendar.py`. |
| `minnesota_home_garden_yields.csv` | Measured harvest per square foot for 26 crops in real home gardens | University of Minnesota Extension, [Home Garden Productivity Project](https://www.ramseymastergardeners.org/post/home-garden-productivity-study-results) (2018-2022, 89 Master Gardeners) | 2026-11-11 | Minnesota has one short season; North Alabama has spring and fall seasons. "Servings" are MyPlate servings as reported by the study. |
| `dga_2025_2030_produce_cups.csv` | Daily vegetable and fruit cups at 12 calorie levels (1,000 to 3,200) | [Dietary Guidelines for Americans 2025-2030, Daily Servings by Calorie Level](https://www.cacfp.org/assets/pdf/Dietary+Guidelines+for+Americans%2C+2025%E2%80%932030_+Daily+Servings+by+Calorie+Level/) | 2026-11-10 | A produce serving is 1 cup raw or cooked, 2 cups leafy greens, or 1/2 cup dried fruit. Used for independence targets only (Phase 4). |

Not yet here (Phase 0 loads them as snapshots): USDA ERS fruit and vegetable prices, USDA
FoodKeeper storage times, a USDA FoodData Central subset for the first crop set, NOAA freeze
dates for the Muscle Shoals 2 N station. Sources and refresh rules are in `docs/design.md`.
