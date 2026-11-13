"""Seed loaders (Phase 0, P0-4) against real Postgres, with small fixtures and no network.

Every test runs on one connection whose outer transaction is rolled back at the end. Sessions join
it with savepoints, so loaders may flush and the command line may even commit without leaving
anything in the shared development database.
"""

import csv
import dataclasses
import json
import shutil
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from prefect.testing.utilities import prefect_test_harness
from sqlmodel import Session, col, delete, func, select

import app.seed.__main__ as seed_cli
from app.core.db import engine
from app.models import (
    CatalogItem,
    CatalogNutrient,
    CatalogPortion,
    CropProfile,
    DataSnapshot,
    PlantingWindow,
    PriceObservation,
    StorageReference,
)
from app.seed import sources
from app.seed.crosswalk import (
    CROSSWALK_PATH,
    CrosswalkRow,
    CupWeightRow,
    PortionMapRow,
    crosswalk_fingerprint,
    ensure_catalog,
    read_crosswalk,
    read_cup_weights,
    read_portion_map,
)
from app.seed.loaders import (
    ErsRow,
    ers_price_per_cup,
    load_ers_prices,
    load_fdc_subset,
    load_foodkeeper,
    load_garden_yields,
    load_planting_calendar,
    load_sr28_portions,
    load_usda_zip,
    month_day,
    purchased_grams,
    read_ers_csv,
    storage_days_min,
    storage_range,
)
from app.seed.report import coverage_gaps, format_report
from app.seed.snapshots import LoadResult, register_snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "seed"
REPO = Path(__file__).resolve().parents[2]
ERS_SOURCES = sorted((REPO / "data" / "sources" / "usda-ers").glob("*.csv"))

# The crosswalk rows the fixtures cover. Stand-ins must be included with the items they serve.
SUBSET = {
    "tomato",
    "zucchini",
    "yellow-squash",
    "collards",
    "turnip",
    "turnip-greens",
    "beet",
    "potato",
    "black-bean",
    "butternut-squash",
    "acorn-squash",
}
FDC_FOUNDATION = {
    r.slug: r.fdc_id
    for r in read_crosswalk()
    if r.slug in SUBSET and r.fdc_dataset == "foundation"
}
FDC_SR_LEGACY = {
    r.slug: r.fdc_id
    for r in read_crosswalk()
    if r.slug in SUBSET and r.fdc_dataset == "sr_legacy"
}
# ERS's published price-per-cup column is swapped within these pairs of rows: each row's own
# price, yield, and cup size give the other row's published value (checked 2026-11-11).
ERS_SWAPPED_PAIRS = [
    (("Artichoke", "Fresh"), ("Artichoke", "Canned")),
    (("Lima beans", "Frozen"), ("Lima beans", "Dried")),
]

SessionFactory = Callable[[], Session]


@pytest.fixture
def session_factory() -> Iterator[SessionFactory]:
    connection = engine.connect()
    outer = connection.begin()

    def factory() -> Session:
        return Session(bind=connection, join_transaction_mode="create_savepoint")

    yield factory
    outer.rollback()
    connection.close()


@pytest.fixture
def session(session_factory: SessionFactory) -> Iterator[Session]:
    with session_factory() as session:
        yield session


@pytest.fixture
def crosswalk() -> list[CrosswalkRow]:
    return [row for row in read_crosswalk() if row.slug in SUBSET]


def _fdc_zip(
    tmp_path: Path, dataset: str, foods: dict[str, str], extra_ids: tuple[str, ...] = ()
) -> Path:
    """A miniature FoodData Central CSV download with the real file layout."""
    folder = f"FoodData_Central_{dataset}_food_csv_test"
    marker = "sr_legacy_food.csv" if dataset == "sr_legacy" else "foundation_food.csv"
    ids = [*foods.values(), *extra_ids]
    files = {
        marker: "fdc_id\n" + "\n".join(ids) + "\n",
        "food.csv": "fdc_id,description\n"
        + "\n".join(f"{i},Food {i}" for i in ids)
        + "\n",
        "nutrient.csv": "id,name,unit_name,nutrient_nbr,rank\n"
        "1003,Protein,G,203,600\n1008,Energy,KCAL,208,300\n",
        "food_nutrient.csv": "id,fdc_id,nutrient_id,amount\n"
        + "".join(f"{n}1,{i},1003,1.5\n{n}2,{i},1008,\n" for n, i in enumerate(ids)),
    }
    path = tmp_path / f"{folder}.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            archive.writestr(f"{folder}/{name}", text)
    return path


# SR28 rows for the miniature download: (NDB, refuse %) and (NDB, seq, amount, measure, grams).
SR28_FOODS = [("11529", "9"), ("11477", "5"), ("11278", "14")]
SR28_WEIGHTS = [
    ("11529", "6", "1", 'medium whole (2-3/5" dia)', "123"),
    ("11529", "1", "1", "cup, chopped or sliced", "180"),
    ("11477", "4", "1", "medium", "196"),
    ("11278", "2", "8", 'pods (3" long)', "95"),
]


def _sr28_zip(
    tmp_path: Path,
    foods: list[tuple[str, str]] = SR28_FOODS,
    weights: list[tuple[str, str, str, str, str]] = SR28_WEIGHTS,
) -> Path:
    """A miniature SR28 ASCII download: caret-separated, text in tildes, CRLF, Latin-1."""

    def line(fields: list[str], text: set[int]) -> str:
        return "^".join(f"~{f}~" if i in text else f for i, f in enumerate(fields))

    food_des = [
        line([ndb, "1100", f"Food {ndb}", "", "", "", "", "Refuse", refuse, "", "6.25"], {0, 1, 2, 3, 4, 5, 6, 7, 9})
        for ndb, refuse in foods
    ]  # fmt: skip
    weight = [line([*w, "", ""], {0, 3}) for w in weights]
    path = tmp_path / "sr28asc.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "FOOD_DES.txt", ("\r\n".join(food_des) + "\r\n").encode("latin-1")
        )
        archive.writestr("WEIGHT.txt", ("\r\n".join(weight) + "\r\n").encode("latin-1"))
    return path


def _portion(
    slug: str, unit: str, size: str, ndb: str, seq: str, refuse: str = ""
) -> PortionMapRow:
    return PortionMapRow(slug, unit, size, ndb, seq, refuse, "", "")


@pytest.fixture
def portions() -> list[PortionMapRow]:
    return [
        _portion("tomato", "each", "medium", "11529", "6"),
        _portion("zucchini", "each", "medium", "11477", "4", refuse="10"),
        _portion(
            "beet", "each", "", "11278", "2"
        ),  # borrows okra's 8-pod row: amount > 1
        _portion("butternut-squash", "each", "", "", ""),  # sold by count, no weight
    ]


def _count(session: Session, model: Any, snapshot: DataSnapshot) -> int:
    statement = select(func.count()).select_from(model)
    return session.exec(statement.where(model.snapshot_id == snapshot.id)).one()


# ---------- snapshots ----------


def test_register_snapshot_is_idempotent_by_hash(
    session: Session, tmp_path: Path
) -> None:
    path = tmp_path / "data.csv"
    path.write_text("a,b\n1,2\n")
    first, created = register_snapshot(
        session,
        source="ers_prices",
        path=path,
        info=sources.ERS_PRICES,
        row_count=1,
        crosswalk_sha256=None,
    )
    again, created_again = register_snapshot(
        session,
        source="ers_prices",
        path=path,
        info=sources.ERS_PRICES,
        row_count=1,
        crosswalk_sha256=None,
    )
    assert created and not created_again
    assert again.id == first.id

    path.write_text("a,b\n1,3\n")  # a new version of the file is a new snapshot
    changed, created_changed = register_snapshot(
        session,
        source="ers_prices",
        path=path,
        info=sources.ERS_PRICES,
        row_count=1,
        crosswalk_sha256=None,
    )
    assert created_changed and changed.id != first.id


def test_a_changed_crosswalk_is_a_new_snapshot_that_applies_the_new_mapping(
    session: Session, crosswalk: list[CrosswalkRow]
) -> None:
    path = FIXTURES / "ers_vegetables.csv"
    first = load_ers_prices(session, path, crosswalk=crosswalk)
    changed = [
        dataclasses.replace(row, ers_form="Canned") if row.slug == "potato" else row
        for row in crosswalk
    ]
    second = load_ers_prices(session, path, crosswalk=changed)
    again = load_ers_prices(session, path, crosswalk=changed)

    assert second.created and second.snapshot.id != first.snapshot.id
    assert second.snapshot.sha256 == first.snapshot.sha256  # same file
    assert not again.created  # same file and same crosswalk: nothing to do
    potato = ensure_catalog(session, changed)["potato"]
    assert potato.conversion_source.endswith("Potatoes (Canned)")
    assert potato.snapshot_id == second.snapshot.id


def test_crosswalk_fingerprint_ignores_row_order(crosswalk: list[CrosswalkRow]) -> None:
    assert crosswalk_fingerprint(crosswalk) == crosswalk_fingerprint(crosswalk[::-1])
    renamed = [dataclasses.replace(crosswalk[0], name="Changed"), *crosswalk[1:]]
    assert crosswalk_fingerprint(renamed) != crosswalk_fingerprint(crosswalk)


# ---------- every loader: twice on the same file creates one snapshot and no duplicates ----------

LOADED_MODELS: dict[str, type] = {
    "ers": PriceObservation,
    "planting": PlantingWindow,
    "yields": CropProfile,
    "fdc": CatalogNutrient,
    "sr28": CatalogPortion,
}


@pytest.mark.parametrize(
    "loader", ["ers", "planting", "yields", "foodkeeper", "fdc", "sr28"]
)
def test_loading_the_same_file_twice_changes_nothing(
    session: Session,
    crosswalk: list[CrosswalkRow],
    portions: list[PortionMapRow],
    tmp_path: Path,
    loader: str,
) -> None:
    def load() -> LoadResult:
        if loader == "ers":
            return load_ers_prices(
                session, FIXTURES / "ers_vegetables.csv", crosswalk=crosswalk
            )
        if loader == "planting":
            return load_planting_calendar(
                session, FIXTURES / "planting_calendar.csv", crosswalk=crosswalk
            )
        if loader == "yields":
            return load_garden_yields(
                session, FIXTURES / "garden_yields.csv", crosswalk=crosswalk
            )
        if loader == "foodkeeper":
            return load_foodkeeper(
                session, FIXTURES / "foodkeeper.json", crosswalk=crosswalk
            )
        if loader == "sr28":
            return load_sr28_portions(
                session, sr28, crosswalk=crosswalk, portions=portions
            )
        return load_fdc_subset(session, fdc, crosswalk=crosswalk)

    fdc = _fdc_zip(tmp_path, "foundation", FDC_FOUNDATION)
    sr28 = _sr28_zip(tmp_path)
    first = load()
    catalog_size = session.exec(select(func.count()).select_from(CatalogItem)).one()
    rows = (
        _count(session, LOADED_MODELS[loader], first.snapshot)
        if loader in LOADED_MODELS
        else 0
    )
    second = load()

    assert first.created and first.rows_written > 0
    assert not second.created and second.rows_written == 0
    assert second.snapshot.id == first.snapshot.id
    assert (
        session.exec(select(func.count()).select_from(CatalogItem)).one()
        == catalog_size
    )
    if loader in LOADED_MODELS:
        assert _count(session, LOADED_MODELS[loader], first.snapshot) == rows


# ---------- catalog from the crosswalk ----------


def test_every_catalog_item_has_a_conversion_source_and_stand_ins_point_at_their_item(
    session: Session,
) -> None:
    crosswalk = read_crosswalk()
    catalog = ensure_catalog(session, crosswalk)
    assert len(catalog) == 176
    assert all(item.conversion_source for item in catalog.values())
    for row in crosswalk:
        item = catalog[row.slug]
        if row.stand_in_for:
            assert item.stand_in_for_id == catalog[row.stand_in_for].id
            assert row.stand_in_for in item.conversion_source
        else:
            assert item.stand_in_for_id is None
    assert catalog["eggplant"].conversion_source.startswith("none: no USDA ERS")
    assert catalog["compost"].conversion_source == "not applicable: garden supply"


# ERS rows that price the same purchase prepared another way: one catalog item per thing bought.
ERS_PREPARATION_VARIANTS = {
    ("Carrots, cooked whole", "Fresh"),
    ("Cucumbers without peel", "Fresh"),
    ("Spinach, boiled", "Fresh"),
}


def test_every_ers_row_is_in_the_crosswalk_once() -> None:
    """The catalog covers all of ERS's fruit and vegetable prices (dogfooding, 2026-11-12)."""
    ers = {
        (row.item, row.form)
        for path in sorted((REPO / "data" / "sources" / "usda-ers").glob("*.csv"))
        for row in read_ers_csv(path)
    }
    mapped = [(r.ers_item, r.ers_form) for r in read_crosswalk() if r.ers_item]
    assert len(mapped) == len(set(mapped)), "an ERS row is mapped twice"
    assert set(mapped) <= ers, "the crosswalk names an ERS row that does not exist"
    assert ers - set(mapped) == ERS_PREPARATION_VARIANTS


def test_crosswalk_rejects_unknown_stand_ins(tmp_path: Path) -> None:
    path = tmp_path / "crosswalk.csv"
    rows = list(csv.DictReader(CROSSWALK_PATH.open()))
    rows[0]["stand_in_for"] = "no-such-slug"
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(ValueError, match="no-such-slug"):
        read_crosswalk(path)


# ---------- USDA ERS ----------


def test_price_per_cup_matches_ers_for_every_row() -> None:
    """Within 0.001 USD of ERS's own column for every row, except two pairs ERS swapped."""
    rows = {(r.item, r.form): r for path in ERS_SOURCES for r in read_ers_csv(path)}
    assert len(rows) == 155
    swapped = {key for pair in ERS_SWAPPED_PAIRS for key in pair}
    worst = max(
        abs(ers_price_per_cup(row) - row.published_price_per_cup)
        for key, row in rows.items()
        if key not in swapped
    )
    assert worst <= 0.001
    for a, b in ERS_SWAPPED_PAIRS:
        assert ers_price_per_cup(rows[a]) == pytest.approx(
            rows[b].published_price_per_cup, abs=0.001
        )
        assert ers_price_per_cup(rows[b]) == pytest.approx(
            rows[a].published_price_per_cup, abs=0.001
        )


@pytest.mark.parametrize(
    ("item", "form", "expected"),
    [  # docs/specs/known-answers.md
        ("Potatoes", "Fresh", 0.300503543),
        ("Black beans", "Dried", 0.259142243),
        ("Collard greens", "Fresh", 0.613558),
        ("Tomatoes, large round", "Fresh", 0.882356978),
        ("Okra", "Fresh", 2.603951096),
        ("Sweet potatoes", "Fresh", 0.582313262),
    ],
)
def test_known_answers(item: str, form: str, expected: float) -> None:
    rows = {(r.item, r.form): r for path in ERS_SOURCES for r in read_ers_csv(path)}
    assert ers_price_per_cup(rows[(item, form)]) == pytest.approx(expected, abs=0.001)


def test_ers_loader_writes_conversions_and_public_prices(
    session: Session, crosswalk: list[CrosswalkRow]
) -> None:
    result = load_ers_prices(
        session, FIXTURES / "ers_vegetables.csv", crosswalk=crosswalk
    )
    catalog = ensure_catalog(session, crosswalk)
    potato = catalog["potato"]
    assert potato.preparation_yield_factor == 0.8113
    assert potato.cup_equivalent_lb == 0.2646
    assert potato.snapshot_id == result.snapshot.id
    assert (
        potato.conversion_source
        == "USDA ERS Fruit and Vegetable Prices 2023: Potatoes (Fresh)"
    )
    # Dried beans weigh more cooked than purchased.
    assert catalog["black-bean"].preparation_yield_factor == 2.4692
    # Beets are canned only in ERS, so the crosswalk names no ERS row: nothing is borrowed.
    assert catalog["beet"].preparation_yield_factor is None

    # Filter by this load's snapshot: a seeded database already has the real ERS potato price.
    price = session.exec(
        select(PriceObservation)
        .where(PriceObservation.catalog_item_id == potato.id)
        .where(PriceObservation.snapshot_id == result.snapshot.id)
    ).one()
    assert price.household_id is None
    assert (price.captured_via, price.source_type, price.visibility) == (
        "public_dataset",
        "public_data",
        "shared",
    )
    assert price.price_cents == 92 and price.unit == "lb" and price.quantity == 1
    assert price.raw_text == "Potatoes (Fresh): 0.921544199 per pound"
    assert result.rows_written == 4  # potato, black bean, collards, zucchini


def test_ers_loader_rejects_non_pound_rows(session: Session, tmp_path: Path) -> None:
    path = tmp_path / "fruit.csv"
    path.write_text(
        "Fruit,Form,AverageRetailPrice,AverageRetailPriceUnitOfMeasure,PreparationYieldFactor,"
        "SizeOfACupEquivalent ,CupEquivalentUnitOfMeasure,AveragePricePerCupEquivalent\n"
        "Watermelon,Fresh,0.5,per bushel,1,0.5,bushels,0.25\n"
    )
    watermelon = [row for row in read_crosswalk() if row.slug == "watermelon"]
    with pytest.raises(ValueError, match="not priced by the pound or the pint"):
        load_ers_prices(session, path, crosswalk=watermelon)


def test_ers_loader_measures_juices_by_the_pint(
    session: Session, tmp_path: Path
) -> None:
    path = tmp_path / "fruit.csv"
    path.write_text(
        "Fruit,Form,AverageRetailPrice,AverageRetailPriceUnitOfMeasure,PreparationYieldFactor,"
        "SizeOfACupEquivalent ,CupEquivalentUnitOfMeasure,AveragePricePerCupEquivalent\n"
        '"Oranges, frozen concentrate",Juice,3.401422791,per pint (16 fluid ounces concentrate),4,0.5,pints,0.425177849\n'
    )
    (entry,) = [
        row for row in read_crosswalk() if row.slug == "orange-juice-concentrate"
    ]

    result = load_ers_prices(session, path, crosswalk=[entry])

    item = session.exec(select(CatalogItem).where(CatalogItem.slug == entry.slug)).one()
    assert (
        item.cup_equivalent_pints,
        item.cup_equivalent_lb,
        item.preparation_yield_factor,
    ) == (0.5, None, 4)
    price = session.exec(
        select(PriceObservation).where(
            PriceObservation.snapshot_id == result.snapshot.id
        )
    ).one()
    assert (price.quantity, price.unit, price.price_cents) == (1, "pint", 340)


def test_ers_reader_strips_the_padded_header() -> None:
    row = read_ers_csv(FIXTURES / "ers_vegetables.csv")[0]
    assert isinstance(row, ErsRow) and row.cup_size > 0


# ---------- planting calendar ----------


def test_planting_windows_are_derived_unverified_and_shared_by_crosswalk(
    session: Session, crosswalk: list[CrosswalkRow]
) -> None:
    result = load_planting_calendar(
        session, FIXTURES / "planting_calendar.csv", crosswalk=crosswalk
    )
    windows = session.exec(
        select(CatalogItem.slug, PlantingWindow.season, PlantingWindow.start_md)
        .join(CatalogItem, col(CatalogItem.id) == col(PlantingWindow.catalog_item_id))
        .where(PlantingWindow.snapshot_id == result.snapshot.id)
    ).all()
    slugs = {slug for slug, _, _ in windows}
    # "Squash, Summer" feeds zucchini and yellow squash; "Turnips" feeds roots and greens.
    assert {"zucchini", "yellow-squash", "turnip", "turnip-greens", "tomato"} <= slugs
    assert ("zucchini", "spring", "04-11") in windows
    unverified = session.exec(
        select(func.count())
        .select_from(PlantingWindow)
        .where(PlantingWindow.snapshot_id == result.snapshot.id)
        .where(col(PlantingWindow.verified).is_(True))
    ).one()
    assert unverified == 0


@pytest.mark.parametrize(
    ("text", "expected"), [("Apr 11", "04-11"), ("Dec 1", "12-01")]
)
def test_month_day(text: str, expected: str) -> None:
    assert month_day(text) == expected


@pytest.mark.parametrize("text", ["April 11", "Apr", "Foo 1", "4/11"])
def test_month_day_rejects_other_formats(text: str) -> None:
    with pytest.raises(ValueError, match="not a month and day"):
        month_day(text)


# ---------- garden yields ----------


def test_one_minnesota_crop_can_feed_several_items(
    session: Session, crosswalk: list[CrosswalkRow]
) -> None:
    result = load_garden_yields(
        session, FIXTURES / "garden_yields.csv", crosswalk=crosswalk
    )
    profiles = dict(
        session.exec(
            select(CatalogItem.slug, CropProfile.yield_lb_per_sq_ft)
            .join(CatalogItem, col(CatalogItem.id) == col(CropProfile.catalog_item_id))
            .where(CropProfile.snapshot_id == result.snapshot.id)
        ).all()
    )
    assert (
        profiles["butternut-squash"] == profiles["acorn-squash"]
    )  # both "Winter Squash"
    assert set(profiles) == {"butternut-squash", "acorn-squash", "tomato", "zucchini"}


# ---------- FoodKeeper ----------


def test_foodkeeper_sets_storage_minimum_and_location(
    session: Session, crosswalk: list[CrosswalkRow]
) -> None:
    result = load_foodkeeper(session, FIXTURES / "foodkeeper.json", crosswalk=crosswalk)
    assert result.snapshot.version == "FMA-Data-test"
    catalog = ensure_catalog(session, crosswalk)
    assert (
        catalog["zucchini"].storage_days_min,
        catalog["zucchini"].storage_location,
    ) == (
        4,
        "refrigerator",
    )
    assert catalog["black-bean"].storage_days_min == 365
    assert catalog["potato"].storage_days_min == 30
    assert (
        catalog["tomato"].storage_days_min is None
    )  # FoodKeeper: "When Ripe", no number
    assert catalog["zucchini"].storage_snapshot_id == result.snapshot.id


def test_foodkeeper_keeps_the_range_and_cooked_leftovers(
    session: Session, crosswalk: list[CrosswalkRow]
) -> None:
    """Phase 1, D4: spoilage uses the range midpoint, and cooked leftovers for freeze extras."""
    result = load_foodkeeper(session, FIXTURES / "foodkeeper.json", crosswalk=crosswalk)
    catalog = ensure_catalog(session, crosswalk)
    zucchini = catalog["zucchini"]
    assert (zucchini.storage_days_min, zucchini.storage_days_max) == (4, 5)
    assert catalog["tomato"].storage_days_max is None
    references = {
        r.location: (r.days_min, r.days_max)
        for r in session.exec(
            select(StorageReference).where(
                StorageReference.snapshot_id == result.snapshot.id
            )
        )
    }
    assert references == {"refrigerator": (3, 4), "freezer": (30, 60)}


def test_a_new_foodkeeper_loader_version_is_a_new_snapshot(
    session: Session, crosswalk: list[CrosswalkRow], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading more from the same file must reload it, even with the same crosswalk."""
    import app.seed.loaders as loaders

    path = FIXTURES / "foodkeeper.json"
    first = load_foodkeeper(session, path, crosswalk=crosswalk)
    monkeypatch.setattr(loaders, "FOODKEEPER_LOADER_VERSION", "3: something more")
    second = load_foodkeeper(session, path, crosswalk=crosswalk)
    assert second.created and second.snapshot.id != first.snapshot.id


def test_foodkeeper_without_cooked_leftovers_is_an_error(
    session: Session, crosswalk: list[CrosswalkRow], tmp_path: Path
) -> None:
    workbook = json.loads((FIXTURES / "foodkeeper.json").read_text(encoding="utf-8"))
    product = next(s for s in workbook["sheets"] if s["name"] == "Product")
    product["data"] = [row for row in product["data"] if row[0].get("ID") != 174.0]
    path = tmp_path / "foodkeeper.json"
    path.write_text(json.dumps(workbook), encoding="utf-8")
    with pytest.raises(ValueError, match="174 \\(cooked leftovers\\) not found"):
        load_foodkeeper(session, path, crosswalk=crosswalk)


@pytest.mark.parametrize(
    ("product", "expected"),
    [
        ({"DOP_Refrigerate_Min": 1, "DOP_Refrigerate_Max": 2, "DOP_Refrigerate_Metric": "Weeks"}, (7, 14)),
        ({"DOP_Refrigerate_Min": 3, "DOP_Refrigerate_Metric": "Days"}, (3, 3)),
        ({"Refrigerate_Min": 3, "Refrigerate_Max": 4, "Refrigerate_Metric": "Days"}, (3, 4)),
        ({"DOP_Refrigerate_Metric": "When Ripe"}, None),
    ],
)  # fmt: skip
def test_storage_range(
    product: dict[str, Any], expected: tuple[int, int] | None
) -> None:
    assert storage_range(product, "refrigerator") == expected


def test_foodkeeper_missing_product_is_an_error(session: Session) -> None:
    eggplant = [row for row in read_crosswalk() if row.slug == "eggplant"]
    with pytest.raises(ValueError, match="FoodKeeper product 284 not found"):
        load_foodkeeper(session, FIXTURES / "foodkeeper.json", crosswalk=eggplant)


def test_storage_days_prefers_date_of_purchase_then_general() -> None:
    both = {"DOP_Refrigerate_Min": 2, "DOP_Refrigerate_Metric": "Weeks",
            "Refrigerate_Min": 1, "Refrigerate_Metric": "Days"}  # fmt: skip
    assert storage_days_min(both, "refrigerator") == 14
    general = {"Refrigerate_Min": 3, "Refrigerate_Metric": "Days"}
    assert storage_days_min(general, "refrigerator") == 3
    assert storage_days_min({"Pantry_Metric": "When Ripe"}, "pantry") is None


# ---------- FoodData Central ----------


@pytest.mark.parametrize(
    ("dataset", "foods", "source"),
    [
        ("foundation", FDC_FOUNDATION, "fdc_foundation"),
        ("sr_legacy", FDC_SR_LEGACY, "fdc_sr_legacy"),
    ],
)
def test_fdc_loads_only_its_own_dataset_and_skips_blank_amounts(
    session: Session,
    crosswalk: list[CrosswalkRow],
    tmp_path: Path,
    dataset: str,
    foods: dict[str, str],
    source: str,
) -> None:
    result = load_fdc_subset(
        session, _fdc_zip(tmp_path, dataset, foods), crosswalk=crosswalk
    )
    assert result.snapshot.source == source
    nutrients = session.exec(
        select(CatalogItem.slug, CatalogNutrient.nutrient, CatalogNutrient.unit)
        .join(CatalogItem, col(CatalogItem.id) == col(CatalogNutrient.catalog_item_id))
        .where(CatalogNutrient.snapshot_id == result.snapshot.id)
    ).all()
    # One protein row per food; the blank energy amounts are skipped.
    assert sorted(nutrients) == sorted((slug, "Protein", "G") for slug in foods)


def test_items_sharing_one_fdc_food_each_get_its_nutrients(
    session: Session, tmp_path: Path
) -> None:
    """Broccoli and broccoli florets name the same FDC food; both get its nutrients."""
    shared = [r for r in read_crosswalk() if r.slug in ("broccoli", "broccoli-florets")]
    assert len({r.fdc_id for r in shared}) == 1
    result = load_fdc_subset(
        session,
        _fdc_zip(tmp_path, "foundation", {"broccoli": shared[0].fdc_id}),
        crosswalk=shared,
    )
    slugs = session.exec(
        select(CatalogItem.slug, CatalogNutrient.nutrient)
        .join(CatalogItem, col(CatalogItem.id) == col(CatalogNutrient.catalog_item_id))
        .where(CatalogNutrient.snapshot_id == result.snapshot.id)
    ).all()
    assert sorted(slug for slug, _ in slugs) == ["broccoli", "broccoli-florets"]
    assert result.rows_written == 2


def test_fdc_id_missing_from_the_download_is_an_error(
    session: Session, crosswalk: list[CrosswalkRow], tmp_path: Path
) -> None:
    partial = dict(list(FDC_FOUNDATION.items())[:1])
    with pytest.raises(ValueError, match="not in"):
        load_fdc_subset(
            session, _fdc_zip(tmp_path, "foundation", partial), crosswalk=crosswalk
        )


def test_fdc_rejects_other_zips(session: Session, tmp_path: Path) -> None:
    path = tmp_path / "other.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("readme.txt", "hello")
    with pytest.raises(ValueError, match="not a FoodData Central"):
        load_fdc_subset(session, path)


def test_fdc_reports_missing_member_files(session: Session, tmp_path: Path) -> None:
    path = tmp_path / "FoodData_Central_foundation_food_csv_broken.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "x/foundation_food.csv", "fdc_id\n"
        )  # like the 2026-10-29 release
    with pytest.raises(ValueError, match="food.csv is missing"):
        load_fdc_subset(session, path)


# ---------- coverage report ----------


def test_report_lists_the_expected_gaps(
    session: Session,
    crosswalk: list[CrosswalkRow],
    portions: list[PortionMapRow],
    tmp_path: Path,
) -> None:
    # Start from an empty national catalog (rolled back with the test).
    session.exec(delete(CatalogItem).where(col(CatalogItem.scope) == "national"))
    assert [g.missing for g in coverage_gaps(session, crosswalk, portions)][0] == [
        "not loaded"
    ]

    load_ers_prices(session, FIXTURES / "ers_vegetables.csv", crosswalk=crosswalk)
    load_foodkeeper(session, FIXTURES / "foodkeeper.json", crosswalk=crosswalk)
    load_fdc_subset(
        session, _fdc_zip(tmp_path, "foundation", FDC_FOUNDATION), crosswalk=crosswalk
    )
    load_fdc_subset(
        session, _fdc_zip(tmp_path, "sr_legacy", FDC_SR_LEGACY), crosswalk=crosswalk
    )
    load_sr28_portions(
        session, _sr28_zip(tmp_path), crosswalk=crosswalk, portions=portions
    )

    gaps = {
        gap.slug: gap.missing for gap in coverage_gaps(session, crosswalk, portions)
    }
    assert gaps["yellow-squash"] == [
        "price"
    ]  # a stand-in: conversions come from zucchini
    assert gaps["beet"] == ["price", "conversion"]  # canned-only in ERS
    assert "storage" in gaps["tomato"]  # FoodKeeper has no number for tomatoes
    assert "zucchini" not in gaps and "black-bean" not in gaps
    # Sold by count with no published weight; acorn squash is not in this portion map at all.
    assert "portion" in gaps["butternut-squash"]
    assert "portion" not in gaps.get("acorn-squash", [])
    assert "portion" not in gaps.get("tomato", [])  # loaded from the SR28 fixture
    assert "beet" in format_report(coverage_gaps(session, crosswalk, portions))


def test_report_with_no_gaps() -> None:
    assert format_report([]).startswith("Every catalog produce item")


# ---------- command line ----------


def test_committed_sources_exist() -> None:
    for _, path in seed_cli.COMMITTED:
        assert path.is_file(), path


def test_load_files_commits_each_file(session_factory: SessionFactory) -> None:
    summaries = seed_cli.load_files(
        [("garden_yields", FIXTURES / "garden_yields.csv")] * 2, session_factory
    )
    assert "rows written" in summaries[0]
    assert "already loaded" in summaries[1]


def test_seed_flow_runs_in_prefect(
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seed_cli, "_default_session", session_factory)
    with prefect_test_harness():
        summaries = seed_cli.seed_flow(
            [("garden_yields", FIXTURES / "garden_yields.csv")]
        )
    assert len(summaries) == 1


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["ers_prices", "a.csv", "b.csv"], [("ers_prices", Path("a.csv")), ("ers_prices", Path("b.csv"))]),
        (["all", "x.zip"], [*seed_cli.COMMITTED, ("usda_zip", Path("x.zip"))]),
    ],
)  # fmt: skip
def test_cli_builds_jobs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    expected: list[tuple[str, Path]],
) -> None:
    seen: list[list[tuple[str, Path]]] = []

    def fake_flow(jobs: list[tuple[str, Path]]) -> list[str]:
        seen.append(jobs)
        return ["done"]

    monkeypatch.setattr(seed_cli, "seed_flow", fake_flow)
    assert seed_cli.main(argv) == 0
    assert seen == [expected]
    assert "done" in capsys.readouterr().out


def test_cli_report(
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(seed_cli, "_default_session", session_factory)
    assert seed_cli.main(["report"]) == 0
    assert "catalog produce" in capsys.readouterr().out


def test_snapshots_are_keyed_by_content_not_file_name(
    session: Session, tmp_path: Path
) -> None:
    """Snapshots are keyed by content, not by file name or location."""
    copy = tmp_path / "renamed.csv"
    shutil.copy(FIXTURES / "garden_yields.csv", copy)
    first = load_garden_yields(session, FIXTURES / "garden_yields.csv")
    second = load_garden_yields(session, copy)
    assert not second.created and second.snapshot.id == first.snapshot.id


# ---------- SR28 household measures (Phase 1, P1-3) ----------


@pytest.mark.parametrize(
    ("edible", "refuse", "expected"),
    [
        (164, 18, 200.0),  # large green pepper; SR28 says "2-1/4 per lb", 201.6 g
        (458, 19, 565.4321),  # peeled eggplant, "yield from 1-1/4 lb" (567 g)
        (
            102,
            64,
            283.3333,
        ),  # medium ear of corn: kernels only, 64% husk, silk, and cob
        (301, 0, 301.0),
    ],
)
def test_purchased_grams_divides_out_refuse(
    edible: float, refuse: int, expected: float
) -> None:
    assert purchased_grams(edible, refuse) == pytest.approx(expected, abs=1e-4)


def test_sr28_portions_store_purchased_weight_with_their_arithmetic(
    session: Session,
    crosswalk: list[CrosswalkRow],
    portions: list[PortionMapRow],
    tmp_path: Path,
) -> None:
    result = load_sr28_portions(
        session, _sr28_zip(tmp_path), crosswalk=crosswalk, portions=portions
    )

    assert result.rows_written == 3  # the butternut row has no source
    loaded = {
        slug: portion
        for portion, slug in session.exec(
            select(CatalogPortion, CatalogItem.slug)
            .join(CatalogItem)
            .where(CatalogPortion.snapshot_id == result.snapshot.id)
        )
    }
    tomato = loaded["tomato"]
    assert (tomato.unit, tomato.size) == ("each", "medium")
    assert tomato.edible_grams == 123 and tomato.refuse_pct == 9
    assert tomato.grams == pytest.approx(123 / 0.91)
    assert tomato.description == 'medium whole (2-3/5" dia)'
    assert "NDB 11529, weight 6" in tomato.source and "9% refuse" in tomato.source
    # A measure for several pieces ("8 pods = 95 g") is divided down to one piece.
    assert loaded["beet"].edible_grams == pytest.approx(95 / 8)
    assert loaded["beet"].grams == pytest.approx(95 / 8 / 0.86)
    # The owner's refuse replaces SR28's (5% in the fixture), and the source says so.
    zucchini = loaded["zucchini"]
    assert zucchini.refuse_pct == 10
    assert zucchini.grams == pytest.approx(196 / 0.9)
    assert "10% refuse (owner override; SR28 gives 5%)" in zucchini.source
    assert "butternut-squash" not in loaded
    assert result.snapshot.source == "usda_sr28"


def test_a_changed_portion_map_is_a_new_snapshot(
    session: Session,
    crosswalk: list[CrosswalkRow],
    portions: list[PortionMapRow],
    tmp_path: Path,
) -> None:
    path = _sr28_zip(tmp_path)
    first = load_sr28_portions(session, path, crosswalk=crosswalk, portions=portions)
    changed = [
        dataclasses.replace(row, size="large") if row.slug == "tomato" else row
        for row in portions
    ]
    second = load_sr28_portions(session, path, crosswalk=crosswalk, portions=changed)

    assert second.created and second.snapshot.id != first.snapshot.id
    sizes = session.exec(
        select(CatalogPortion.size).where(
            CatalogPortion.snapshot_id == second.snapshot.id,
            CatalogPortion.unit == "each",
        )
    ).all()
    assert "large" in sizes


@pytest.mark.parametrize(
    ("foods", "weights", "message"),
    [
        (SR28_FOODS, SR28_WEIGHTS[1:], "not in sr28asc.zip"),  # tomato's weight is gone
        ([("11529", ""), *SR28_FOODS[1:]], SR28_WEIGHTS, "without a refuse value"),
    ],
)
def test_sr28_rows_named_in_the_map_must_exist(
    session: Session,
    crosswalk: list[CrosswalkRow],
    portions: list[PortionMapRow],
    tmp_path: Path,
    foods: list[tuple[str, str]],
    weights: list[tuple[str, str, str, str, str]],
    message: str,
) -> None:
    path = _sr28_zip(tmp_path, foods, weights)
    with pytest.raises(ValueError, match=message):
        load_sr28_portions(session, path, crosswalk=crosswalk, portions=portions)


def test_sr28_rejects_other_zips(session: Session, tmp_path: Path) -> None:
    path = tmp_path / "other.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("FOOD_DES.txt", "")
    with pytest.raises(ValueError, match="not an SR28"):
        load_sr28_portions(session, path)


def test_usda_zips_are_loaded_by_what_they_contain(
    session_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "app.seed.loaders.load_sr28_portions",
        lambda _s, _p: calls.append("sr28") or LoadResult(None, False, 0),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        "app.seed.loaders.load_fdc_subset",
        lambda _s, _p: calls.append("fdc") or LoadResult(None, False, 0),  # type: ignore[arg-type]
    )
    with session_factory() as session:
        load_usda_zip(session, _sr28_zip(tmp_path))
        load_usda_zip(session, _fdc_zip(tmp_path, "foundation", FDC_FOUNDATION))
    assert calls == ["sr28", "fdc"]


def test_the_committed_portion_map_is_valid() -> None:
    """Every row names a crosswalk slug, a known unit and size, and both SR28 keys or neither."""
    rows = read_portion_map()
    assert {row.unit for row in rows} <= {"each", "bunch", "head", "ear", "pint"}
    assert [row.slug for row in rows if not row.has_source] == ["butternut-squash"]
    # Owner decision (2026-11-11): potatoes use 10% refuse, not SR28's 25%.
    assert {row.refuse_override for row in rows if row.slug == "potato"} == {10}
    assert {row.slug for row in rows if row.refuse_override is not None} == {"potato"}
    # Dogfooding: these were priced by count at a market and each asked for a weight.
    priced_by_count = {"avocado", "mango", "kiwi", "clementine", "orange", "grapefruit"}
    priced_by_count |= {"poblano-pepper"}  # owner: its own item, bell pepper weights
    assert priced_by_count <= {row.slug for row in rows if row.has_source}


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("kumquat,each,,11529,6,,,", "not in the crosswalk"),
        ("tomato,dozen,,11529,6,,,", "unknown unit 'dozen'"),
        ("tomato,each,huge,11529,6,,,", "unknown size 'huge'"),
        ("tomato,each,,11529,,,,", "give both sr28_ndb and sr28_seq"),
        ("tomato,each,medium,11529,9,,,", "listed twice"),
        ("tomato,each,large,11529,5,100,,", "must be 0 to 99"),
        ("tomato,each,large,11529,5,ten,,", "must be 0 to 99"),
        ("tomato,each,large,,,10,,", "needs an SR28 weight"),
        # A weight someone gave: SR28 names the food for its refuse only (owner, 2026-11-13).
        ("tomato,each,large,11529,,,abc,", "grams must be a weight above 0"),
        ("tomato,each,large,11529,,,0,", "grams must be a weight above 0"),
        ("tomato,each,large,,,,50,", "grams needs sr28_ndb"),
        ("tomato,each,large,11529,5,,50,", "grams needs sr28_ndb"),
    ],
)
def test_portion_map_rejects_bad_rows(tmp_path: Path, line: str, message: str) -> None:
    path = tmp_path / "catalog_portions.csv"
    path.write_text(
        "slug,unit,size,sr28_ndb,sr28_seq,refuse_pct_override,grams,notes\n"
        "tomato,each,medium,11529,6,,,\n" + line + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=message):
        read_portion_map(path)


# ---------- items compared per piece, and nuts (spec P1-11) ----------

FOODKEEPER_SOURCE = REPO / "data" / "sources" / "usda-fsis" / "foodkeeper.json"
P1_11 = {"lemon", "lime", "garlic", "elephant-garlic", "walnut", "pecan", "hazelnut"}


def test_the_committed_crosswalk_marks_the_items_not_counted_in_cups() -> None:
    """Owner (2026-11-13): lemons and limes each, garlic by the head; none of these (or nuts)
    counts toward produce cups. Every other item stays compared by the cup and counted."""
    rows = {row.slug: row for row in read_crosswalk()}
    assert P1_11 <= set(rows)
    measures = {slug: rows[slug].measure for slug in P1_11}
    assert measures == {
        "lemon": "each", "lime": "each", "garlic": "head", "elephant-garlic": "head",
        "walnut": "cup", "pecan": "cup", "hazelnut": "cup",
    }  # fmt: skip
    assert {s for s, row in rows.items() if not row.counts_toward_produce_cups} == P1_11
    assert all(rows[s].measure == "cup" for s in set(rows) - P1_11)
    assert rows["hazelnut"].storage_as == "walnuts"
    assert rows["hazelnut"].foodkeeper_id == rows["walnut"].foodkeeper_id


def test_the_committed_cup_weights_are_for_the_nuts() -> None:
    assert {row.slug for row in read_cup_weights()} == {"walnut", "pecan", "hazelnut"}


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("kumquat,1,1,", "not in the crosswalk"),
        ("tomato,11529,1,", "has an ERS row or a stand-in"),
        ("lemon,09150,1,", "compared per piece"),
        ("walnut,12155,,", "give sr28_ndb and sr28_seq"),
    ],
)
def test_cup_weights_reject_bad_rows(tmp_path: Path, line: str, message: str) -> None:
    path = tmp_path / "catalog_cup_weights.csv"
    path.write_text("slug,sr28_ndb,sr28_seq,notes\n" + line + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        read_cup_weights(path)


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("compared_by", "dozen", "unknown compared_by"),
        ("produce_cups", "yes", "blank or 'no'"),
    ],
)
def test_the_crosswalk_rejects_unknown_measures(
    tmp_path: Path, column: str, value: str, message: str
) -> None:
    rows = list(csv.DictReader(CROSSWALK_PATH.open(encoding="utf-8")))
    rows[0][column] = value
    path = tmp_path / "catalog_crosswalk.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(ValueError, match=message):
        read_crosswalk(path)


def test_given_weights_and_nut_cups_load_with_their_arithmetic(
    session: Session, tmp_path: Path
) -> None:
    crosswalk = [row for row in read_crosswalk() if row.slug in {"garlic", "walnut"}]
    portions = [
        PortionMapRow(
            "garlic", "head", "medium", "11215", "", "", "47.5", "Owner: softneck"
        )
    ]
    cups = [CupWeightRow("walnut", "12155", "1", "")]
    zip_path = _sr28_zip(
        tmp_path,
        foods=[("11215", "13"), ("12155", "55")],
        weights=[("12155", "1", "1", "cup, chopped", "117")],
    )
    result = load_sr28_portions(
        session, zip_path, crosswalk=crosswalk, portions=portions, cup_weights=cups
    )
    assert result.rows_written == 2
    garlic = session.exec(select(CatalogItem).where(CatalogItem.slug == "garlic")).one()
    (head,) = session.exec(
        select(CatalogPortion).where(CatalogPortion.catalog_item_id == garlic.id)
    ).all()
    assert (head.unit, head.size, head.grams, head.refuse_pct) == (
        "head",
        "medium",
        47.5,
        13,
    )
    assert head.edible_grams == pytest.approx(47.5 * 0.87)
    assert head.source == (
        "One medium head = 47.5 g as bought; 13% refuse from USDA SR28 NDB 11215. "
        "Owner: softneck"
    )
    assert (garlic.compared_by, garlic.produce_cups) == ("head", False)
    assert (
        garlic.conversion_source == "not needed: compared per piece (head), not per cup"
    )
    # Compared per piece, so no cup conversion is missing (the coverage report).
    gaps = {
        gap.slug: gap.missing for gap in coverage_gaps(session, crosswalk, portions)
    }
    assert "conversion" not in gaps.get("garlic", [])
    walnut = session.exec(select(CatalogItem).where(CatalogItem.slug == "walnut")).one()
    assert walnut.preparation_yield_factor == 1.0
    assert walnut.cup_equivalent_lb == pytest.approx(117 / 453.59237)
    assert walnut.shell_refuse_pct == 55 and walnut.produce_cups is False
    assert walnut.snapshot_id == result.snapshot.id
    assert walnut.conversion_source == (
        "USDA SR28 NDB 12155, weight 1: 1 cup, chopped = 117 g of kernels; 55% shell refuse"
    )


def test_a_changed_cup_weight_file_is_a_new_snapshot() -> None:
    crosswalk = read_crosswalk()
    one = crosswalk_fingerprint(
        crosswalk, [], cup_weights=[CupWeightRow("walnut", "12155", "1", "")]
    )
    two = crosswalk_fingerprint(
        crosswalk, [], cup_weights=[CupWeightRow("walnut", "12155", "4", "")]
    )
    assert one != two


def test_storage_borrowed_from_another_food_is_named(session: Session) -> None:
    crosswalk = [row for row in read_crosswalk() if row.slug in {"walnut", "hazelnut"}]
    load_foodkeeper(session, FOODKEEPER_SOURCE, crosswalk=crosswalk)
    items = {
        item.slug: item
        for item in session.exec(
            select(CatalogItem).where(col(CatalogItem.slug).in_(["walnut", "hazelnut"]))
        )
    }
    assert (
        items["hazelnut"].storage_as == "walnuts" and items["walnut"].storage_as is None
    )
    assert (items["hazelnut"].storage_days_min, items["hazelnut"].storage_days_max) == (
        items["walnut"].storage_days_min,
        items["walnut"].storage_days_max,
    )
