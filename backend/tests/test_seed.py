"""Seed loaders (Phase 0, P0-4) against real Postgres, with small fixtures and no network.

Every test runs on one connection whose outer transaction is rolled back at the end. Sessions join
it with savepoints, so loaders may flush and the command line may even commit without leaving
anything in the shared development database.
"""

import csv
import dataclasses
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
    CropProfile,
    DataSnapshot,
    PlantingWindow,
    PriceObservation,
)
from app.seed import sources
from app.seed.crosswalk import (
    CROSSWALK_PATH,
    CrosswalkRow,
    crosswalk_fingerprint,
    ensure_catalog,
    read_crosswalk,
)
from app.seed.loaders import (
    ErsRow,
    ers_price_per_cup,
    load_ers_prices,
    load_fdc_subset,
    load_foodkeeper,
    load_garden_yields,
    load_planting_calendar,
    month_day,
    read_ers_csv,
    storage_days_min,
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
}


@pytest.mark.parametrize("loader", ["ers", "planting", "yields", "foodkeeper", "fdc"])
def test_loading_the_same_file_twice_changes_nothing(
    session: Session, crosswalk: list[CrosswalkRow], tmp_path: Path, loader: str
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
        return load_fdc_subset(session, fdc, crosswalk=crosswalk)

    fdc = _fdc_zip(tmp_path, "foundation", FDC_FOUNDATION)
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
    assert len(catalog) == 44
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
        "Watermelon,Fresh,0.5,per pint,1,0.5,pints,0.25\n"
    )
    watermelon = [row for row in read_crosswalk() if row.slug == "watermelon"]
    with pytest.raises(ValueError, match="not priced by the pound"):
        load_ers_prices(session, path, crosswalk=watermelon)


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
    session: Session, crosswalk: list[CrosswalkRow], tmp_path: Path
) -> None:
    # Start from an empty national catalog (rolled back with the test).
    session.exec(delete(CatalogItem).where(col(CatalogItem.scope) == "national"))
    assert [g.missing for g in coverage_gaps(session, crosswalk)][0] == ["not loaded"]

    load_ers_prices(session, FIXTURES / "ers_vegetables.csv", crosswalk=crosswalk)
    load_foodkeeper(session, FIXTURES / "foodkeeper.json", crosswalk=crosswalk)
    load_fdc_subset(
        session, _fdc_zip(tmp_path, "foundation", FDC_FOUNDATION), crosswalk=crosswalk
    )
    load_fdc_subset(
        session, _fdc_zip(tmp_path, "sr_legacy", FDC_SR_LEGACY), crosswalk=crosswalk
    )

    gaps = {gap.slug: gap.missing for gap in coverage_gaps(session, crosswalk)}
    assert gaps["yellow-squash"] == [
        "price"
    ]  # a stand-in: conversions come from zucchini
    assert gaps["beet"] == ["price", "conversion"]  # canned-only in ERS
    assert "storage" in gaps["tomato"]  # FoodKeeper has no number for tomatoes
    assert "zucchini" not in gaps and "black-bean" not in gaps
    assert "beet" in format_report(coverage_gaps(session, crosswalk))


def test_report_with_no_gaps() -> None:
    assert format_report([]).startswith("Every first-crop-set item")


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
        (["all", "x.zip"], [*seed_cli.COMMITTED, ("fdc", Path("x.zip"))]),
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
    assert "first-crop-set" in capsys.readouterr().out


def test_snapshots_are_keyed_by_content_not_file_name(
    session: Session, tmp_path: Path
) -> None:
    """Snapshots are keyed by content, not by file name or location."""
    copy = tmp_path / "renamed.csv"
    shutil.copy(FIXTURES / "garden_yields.csv", copy)
    first = load_garden_yields(session, FIXTURES / "garden_yields.csv")
    second = load_garden_yields(session, copy)
    assert not second.created and second.snapshot.id == first.snapshot.id
