# Seed data

Reference data the app loads into versioned snapshots (see `docs/design.md`, "Data sources,
caching, and versioning"). Never edit a file here in place without recording why in the
`docs/design.md` change log; loaders store each new file version as a new snapshot.

| File | What it holds | Source | Retrieved | Notes |
| --- | --- | --- | --- | --- |
| `north_alabama_planting_calendar.csv` | 63 spring and fall planting windows for 39 crops: published central-Alabama dates, derived North Alabama dates, days to maturity, spacing | Alabama Cooperative Extension, "Alabama Vegetable Garden Planting Chart" ([planting guide](https://www.aces.edu/blog/topics/lawn-garden/planting-guide-for-home-gardening-in-alabama/)); PDF in `data/sources/alabama-extension/` | 2026-11-11 | North Alabama dates are derived (spring +10 days, fall -10 days) and **unverified**; do not use them as golden answers. Rebuild with `uv run scripts/data/build_planting_calendar.py`. |
| `minnesota_home_garden_yields.csv` | Measured harvest per square foot for 26 crops in real home gardens | University of Minnesota Extension, [Home Garden Productivity Project](https://www.ramseymastergardeners.org/post/home-garden-productivity-study-results) (2018-2022, 89 Master Gardeners) | 2026-11-11 | Minnesota has one short season; North Alabama has spring and fall seasons. "Servings" are MyPlate servings as reported by the study. |
| `dga_2025_2030_produce_cups.csv` | Daily vegetable and fruit cups at 12 calorie levels (1,000 to 3,200) | [Dietary Guidelines for Americans 2025-2030, Daily Servings by Calorie Level](https://www.cacfp.org/assets/pdf/Dietary+Guidelines+for+Americans%2C+2025%E2%80%932030_+Daily+Servings+by+Calorie+Level/) | 2026-11-10 | A produce serving is 1 cup raw or cooked, 2 cups leafy greens, or 1/2 cup dried fruit. Used for independence targets only (Phase 4). |

| `catalog_portions.csv` | For each item sold by count: unit (`each`, `head`, `ear`, `bunch`), size, and the SR28 weight row (NDB number and sequence) that weighs one piece | Built by hand from SR28 `WEIGHT.txt` | 2026-11-11 | **Needs owner review.** A row without SR28 keys marks an item sold by count with no weight (the coverage report lists it). Notes record each judgment call, such as weights left out because SR28's refuse does not fit them. |
| `catalog_crosswalk.csv` | Each catalog slug (38 first-crop-set produce items, 6 garden supplies) and its exact name in every source: ERS item and form, FDC id and dataset, FoodKeeper product id and storage location, Minnesota crop, planting-chart crops (`;`-separated), stand-in | Built by hand from the sources below | 2026-11-11 | **Owner-reviewed** (2026-11-11). Loaders match only the names given here; no fuzzy matching. The notes column explains each judgment call. |

## Raw source downloads

Stored byte for byte (see `.gitattributes`): a file's SHA-256 identifies its data snapshot, so a
changed byte would be a new snapshot. Download a new version, put it next to the old one, update
`app/seed/sources.py`, and add a row here.

| File | Source | Retrieved | SHA-256 (first 12) | Notes |
| --- | --- | --- | --- | --- |
| `data/sources/usda-ers/all-vegetables-average-prices.csv` | [USDA ERS](https://www.ers.usda.gov/media/6240/all-vegetables-average-prices-csv-format.csv?v=47411) | 2026-11-11 | `7228c4bccd6a` | Data year 2023. Byte-order mark, CRLF line endings, and a trailing space in one header. ERS's published price-per-cup column is swapped for Artichoke (Fresh, Canned) and Lima beans (Frozen, Dried); see `docs/specs/known-answers.md`. |
| `data/sources/usda-ers/all-fruits-average-prices.csv` | [USDA ERS](https://www.ers.usda.gov/media/6210/all-fruits-average-prices-csv-format.csv?v=97997) | 2026-11-11 | `b7fc0b4a9c11` | Data year 2023. Juices are priced per pint. |
| `data/sources/usda-fsis/foodkeeper.json` | [USDA FSIS FoodKeeper](https://catalog.data.gov/dataset/fsis-foodkeeper-data) (`FMA-Data-v128`) | 2026-11-11 | `037df0d30112` | **Download by hand in a browser**: FSIS blocks automated downloads. |
| *Not committed*: `FoodData_Central_foundation_food_csv_2026-04-30.zip` | [USDA FDC](https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_foundation_food_csv_2026-04-30.zip) | 2026-11-11 | `70457ee9d934` | 3.8 MB. The 2026-10-29 release was skipped: its zip lacks `food.csv` and `food_nutrient.csv`. |
| *Not committed*: `sr28asc.zip` | [USDA ARS SR28](https://www.ars.usda.gov/ARSUserFiles/80400535/DATA/SR/sr28/dnload/sr28asc.zip) | 2026-11-11 | `8308fd1d224e` | 6.2 MB. Household-measure weights (edible) and refuse percentages. Its weights match FDC's SR Legacy `food_portion.csv` for all 43 mapped rows. |
| *Not committed*: `FoodData_Central_sr_legacy_food_csv_2018-04.zip` | [USDA FDC](https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_sr_legacy_food_csv_2018-04.zip) | 2026-11-11 | `b80817294b88` | 6.1 MB. SR Legacy is final (2018). |

## Loading

From `backend/`, with the database migrated (`scripts/prestart.sh`):

```bash
uv run python -m app.seed all ~/Downloads/FoodData_Central_*.zip ~/Downloads/sr28asc.zip  # every source above
uv run python -m app.seed report                                    # first-crop-set gaps
```

Each file loads in its own transaction inside a Prefect flow. Loading the same file again changes
nothing, unless the crosswalk changed: each snapshot records a fingerprint of the crosswalk rows
it used, so a changed mapping loads as a new snapshot (the latest snapshot of a source is the
current one). Snapshots loaded before fingerprints existed (2026-11-11, PR #13) stay as history. Not yet here: NOAA freeze dates for the Muscle Shoals 2 N station (the region row uses
the values in `docs/design.md`).
