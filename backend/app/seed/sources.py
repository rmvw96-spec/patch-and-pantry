"""Provenance defaults for each dataset, matching app/seed_data/README.md.

Change a default here when a new version of a file is downloaded, and record the download in the
README. The values describe the file, so they are stored on its snapshot.
"""

from datetime import UTC, date, datetime

from app.seed.snapshots import SourceInfo

RETRIEVED = datetime(2026, 11, 11, tzinfo=UTC)

ERS_PRICES = SourceInfo(
    url="https://www.ers.usda.gov/data-products/fruit-and-vegetable-prices",
    version="2023",
    retrieved_at=RETRIEVED,
    notes="Annual average retail prices, data year 2023 (page updated 2025-12-09). "
    "Not valid for year-to-year trends.",
)
# ERS prices are annual averages; price observations get the middle of the data year.
ERS_OBSERVED_ON = date(2023, 7, 1)

FOODKEEPER = SourceInfo(
    url="https://catalog.data.gov/dataset/fsis-foodkeeper-data",
    version="",  # read from the file's fileName (for example FMA-Data-v128)
    retrieved_at=RETRIEVED,
    notes="Downloaded by hand: FSIS blocks automated downloads.",
)

FDC_FOUNDATION = SourceInfo(
    url="https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_foundation_food_csv_2026-04-30.zip",
    version="2026-04-30",
    retrieved_at=RETRIEVED,
    notes="The 2026-10-29 release was skipped: its download lacks food.csv and "
    "food_nutrient.csv.",
)

FDC_SR_LEGACY = SourceInfo(
    url="https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_sr_legacy_food_csv_2018-04.zip",
    version="2018-04",
    retrieved_at=RETRIEVED,
    notes="SR Legacy is final; used where Foundation Foods lacks an item.",
)

PLANTING_CALENDAR = SourceInfo(
    url="https://www.aces.edu/blog/topics/lawn-garden/planting-guide-for-home-gardening-in-alabama/",
    version="2026-11-11",
    retrieved_at=RETRIEVED,
    notes="Central Alabama chart; North Alabama dates derived (spring +10 days, fall -10 "
    "days) and unverified.",
)

GARDEN_YIELDS = SourceInfo(
    url="https://www.ramseymastergardeners.org/post/home-garden-productivity-study-results",
    version="2018-2022 study",
    retrieved_at=RETRIEVED,
    notes="University of Minnesota Extension, Home Garden Productivity Project; one short "
    "Minnesota season.",
)

SR28 = SourceInfo(
    url="https://www.ars.usda.gov/ARSUserFiles/80400535/DATA/SR/sr28/dnload/sr28asc.zip",
    version="SR28 (2015)",
    retrieved_at=RETRIEVED,
    notes="Household-measure weights (edible portion, WEIGHT.txt) and refuse percentages "
    "(FOOD_DES.txt). FoodData Central's SR Legacy CSV has the weights but not the refuse.",
)
