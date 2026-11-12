"""The seed loaders. Each one registers its file as a snapshot, then writes rows from it.

All loaders share the same contract: ``load_x(session, path) -> LoadResult``. They create any
missing national catalog items from the crosswalk first, match source rows only by the names the
crosswalk gives, flush without committing, and write nothing when the file was already loaded.
"""

import csv
import io
import json
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, cast, get_args

from sqlmodel import Session, select

from app.models import (
    CatalogNutrient,
    CatalogPortion,
    CropProfile,
    PlantingWindow,
    PriceObservation,
    Region,
    Season,
    SnapshotSource,
)
from app.seed import sources
from app.seed.crosswalk import (
    CrosswalkRow,
    PortionMapRow,
    crosswalk_fingerprint,
    ensure_catalog,
    read_crosswalk,
    read_portion_map,
)
from app.seed.snapshots import LoadResult, SourceInfo, register_snapshot

# ---------- USDA ERS fruit and vegetable prices ----------


@dataclass(frozen=True)
class ErsRow:
    item: str
    form: str
    price: float  # per pound (or per pint for some juices)
    price_unit: str
    preparation_yield_factor: float
    cup_size: float
    cup_unit: str
    published_price_per_cup: float


def read_ers_csv(path: Path) -> list[ErsRow]:
    """Read an ERS CSV. The files start with a byte-order mark and one header has a trailing space."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = [name.strip() for name in next(reader)]
        rows = [dict(zip(header, values, strict=True)) for values in reader if values]
    item_column = header[0]  # "Vegetable" or "Fruit"
    return [
        ErsRow(
            item=row[item_column],
            form=row["Form"],
            price=float(row["AverageRetailPrice"]),
            price_unit=row["AverageRetailPriceUnitOfMeasure"],
            preparation_yield_factor=float(row["PreparationYieldFactor"]),
            cup_size=float(row["SizeOfACupEquivalent"]),
            cup_unit=row["CupEquivalentUnitOfMeasure"],
            published_price_per_cup=float(row["AveragePricePerCupEquivalent"]),
        )
        for row in rows
    ]


def ers_price_per_cup(row: ErsRow) -> float:
    """Price per cup equivalent. Matches ERS's published column within 0.001 USD (it rounds inputs)."""
    return row.price / row.preparation_yield_factor * row.cup_size


def load_ers_prices(
    session: Session,
    path: Path,
    *,
    info: SourceInfo = sources.ERS_PRICES,
    observed_on: date = sources.ERS_OBSERVED_ON,
    crosswalk: list[CrosswalkRow] | None = None,
) -> LoadResult:
    """Conversion facts for national produce items, and one public-data price per item."""
    crosswalk = crosswalk if crosswalk is not None else read_crosswalk()
    rows = read_ers_csv(path)
    snapshot, created = register_snapshot(
        session,
        source="ers_prices",
        path=path,
        info=info,
        row_count=len(rows),
        crosswalk_sha256=crosswalk_fingerprint(crosswalk),
    )
    if not created:
        return LoadResult(snapshot, False, 0)
    catalog = ensure_catalog(session, crosswalk)
    by_name = {(row.item, row.form): row for row in rows}
    written = 0
    for entry in crosswalk:
        row = by_name.get((entry.ers_item, entry.ers_form))
        if not entry.ers_item or row is None:
            continue  # not an ERS item, or it lives in the other ERS file
        if row.price_unit != "per pound" or row.cup_unit != "pounds":
            raise ValueError(
                f"{entry.slug}: ERS row {row.item} ({row.form}) is not priced by the pound"
            )
        item = catalog[entry.slug]
        item.preparation_yield_factor = row.preparation_yield_factor
        item.cup_equivalent_lb = row.cup_size
        item.conversion_source = f"USDA ERS Fruit and Vegetable Prices {info.version}: {row.item} ({row.form})"
        item.snapshot_id = snapshot.id
        session.add(
            PriceObservation(
                household_id=None,
                catalog_item_id=item.id,
                price_cents=round(row.price * 100),
                quantity=1,
                unit="lb",
                observed_on=observed_on,
                captured_via="public_dataset",
                source_type="public_data",
                visibility="shared",
                is_sale_price=False,
                # The full-precision price, since cents round it.
                raw_text=f"{row.item} ({row.form}): {row.price} {row.price_unit}",
                snapshot_id=snapshot.id,
            )
        )
        written += 1
    session.flush()
    return LoadResult(snapshot, True, written)


# ---------- Alabama Extension planting calendar (North Alabama, derived) ----------

_MONTHS: dict[str, int] = {
    name: number
    for number, name in enumerate(
        "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), start=1
    )
}


def month_day(text: str) -> str:
    """'Apr 11' -> '04-11'."""
    match = re.fullmatch(r"([A-Z][a-z]{2}) (\d{1,2})", text.strip())
    if match is None or match.group(1) not in _MONTHS:
        raise ValueError(f"not a month and day: {text!r}")
    return f"{_MONTHS[match.group(1)]:02d}-{int(match.group(2)):02d}"


def _season(value: str) -> Season:
    if value not in get_args(Season):
        raise ValueError(f"unknown season {value!r}")
    return cast(Season, value)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _region(session: Session, code: str) -> Region:
    region = session.exec(select(Region).where(Region.code == code)).first()
    if region is None:
        raise ValueError(f"region {code!r} is not seeded; run scripts/prestart.sh")
    return region


def load_planting_calendar(
    session: Session,
    path: Path,
    *,
    info: SourceInfo = sources.PLANTING_CALENDAR,
    crosswalk: list[CrosswalkRow] | None = None,
) -> LoadResult:
    """North Alabama planting windows, always ``verified=False`` (derived, not checked)."""
    crosswalk = crosswalk if crosswalk is not None else read_crosswalk()
    rows = _read_csv(path)
    snapshot, created = register_snapshot(
        session,
        source="planting_calendar",
        path=path,
        info=info,
        row_count=len(rows),
        crosswalk_sha256=crosswalk_fingerprint(crosswalk),
    )
    if not created:
        return LoadResult(snapshot, False, 0)
    catalog = ensure_catalog(session, crosswalk)
    region = _region(session, "north-al")
    windows: set[tuple[str, Season, str, str]] = set()
    for entry in crosswalk:
        for row in rows:
            if row["crop"] in entry.planting_chart_crops and row["in_scope"].startswith(
                "yes"
            ):
                windows.add(
                    (
                        entry.slug,
                        _season(row["season"]),
                        month_day(row["north_al_start"]),
                        month_day(row["north_al_end"]),
                    )
                )
    for slug, season, start, end in sorted(windows):
        session.add(
            PlantingWindow(
                catalog_item_id=catalog[slug].id,
                region_id=region.id,
                season=season,
                start_md=start,
                end_md=end,
                verified=False,
                snapshot_id=snapshot.id,
            )
        )
    session.flush()
    return LoadResult(snapshot, True, len(windows))


# ---------- Minnesota home garden yields ----------


def load_garden_yields(
    session: Session,
    path: Path,
    *,
    info: SourceInfo = sources.GARDEN_YIELDS,
    crosswalk: list[CrosswalkRow] | None = None,
) -> LoadResult:
    """Measured yield per square foot, one crop profile per crosswalk item with a Minnesota crop."""
    crosswalk = crosswalk if crosswalk is not None else read_crosswalk()
    rows = {row["crop"]: row for row in _read_csv(path)}
    snapshot, created = register_snapshot(
        session,
        source="garden_yields",
        path=path,
        info=info,
        row_count=len(rows),
        crosswalk_sha256=crosswalk_fingerprint(crosswalk),
    )
    if not created:
        return LoadResult(snapshot, False, 0)
    catalog = ensure_catalog(session, crosswalk)
    written = 0
    for entry in crosswalk:
        row = rows.get(entry.minnesota_crop) if entry.minnesota_crop else None
        if row is None:
            continue
        session.add(
            CropProfile(
                catalog_item_id=catalog[entry.slug].id,
                yield_lb_per_sq_ft=float(row["lb_per_sq_ft"]),
                yield_source=f"{row['source']}: {row['crop']}",
                snapshot_id=snapshot.id,
            )
        )
        written += 1
    session.flush()
    return LoadResult(snapshot, True, written)


# ---------- USDA FSIS FoodKeeper ----------

_DAYS_PER = {"Days": 1, "Weeks": 7, "Months": 30, "Years": 365}
_STORAGE_COLUMN = {"refrigerator": "Refrigerate", "pantry": "Pantry"}


def _sheet(workbook: dict[str, Any], name: str) -> list[dict[str, Any]]:
    """FoodKeeper rows are lists of one-key objects; flatten each into a dict."""
    sheet = next(s for s in workbook["sheets"] if s["name"] == name)
    return [{k: v for cell in row for k, v in cell.items()} for row in sheet["data"]]


def storage_days_min(product: dict[str, Any], location: str) -> int | None:
    """The range minimum in days. Prefers the 'from date of purchase' columns (DOP_)."""
    column = _STORAGE_COLUMN[location]
    for prefix in ("DOP_", ""):
        minimum = product.get(f"{prefix}{column}_Min")
        metric = product.get(f"{prefix}{column}_Metric")
        if minimum is not None and metric in _DAYS_PER:
            return round(float(minimum) * _DAYS_PER[metric])
    return None  # for example tomatoes: "When Ripe", with no number


def load_foodkeeper(
    session: Session,
    path: Path,
    *,
    info: SourceInfo = sources.FOODKEEPER,
    crosswalk: list[CrosswalkRow] | None = None,
) -> LoadResult:
    """Storage time (range minimum, in days) and storage location for each crosswalk item."""
    crosswalk = crosswalk if crosswalk is not None else read_crosswalk()
    workbook = json.loads(path.read_text(encoding="utf-8"))
    products = {int(p["ID"]): p for p in _sheet(workbook, "Product")}
    info = replace(info, version=info.version or Path(workbook["fileName"]).stem)
    snapshot, created = register_snapshot(
        session,
        source="foodkeeper",
        path=path,
        info=info,
        row_count=len(products),
        crosswalk_sha256=crosswalk_fingerprint(crosswalk),
    )
    if not created:
        return LoadResult(snapshot, False, 0)
    catalog = ensure_catalog(session, crosswalk)
    written = 0
    for entry in crosswalk:
        if not entry.foodkeeper_id:
            continue
        product = products.get(int(entry.foodkeeper_id))
        if product is None:
            raise ValueError(
                f"{entry.slug}: FoodKeeper product {entry.foodkeeper_id} not found"
            )
        item = catalog[entry.slug]
        item.storage_days_min = storage_days_min(product, entry.foodkeeper_location)
        item.storage_location = entry.foodkeeper_location
        item.storage_snapshot_id = snapshot.id
        written += 1
    session.flush()
    return LoadResult(snapshot, True, written)


# ---------- USDA FoodData Central (Foundation Foods, or SR Legacy) ----------


def _zip_csv(archive: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    member = next(
        (m for m in archive.namelist() if m.endswith(f"/{name}") or m == name), None
    )
    if member is None:
        raise ValueError(f"{name} is missing from the download")
    with archive.open(member) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))


def fdc_dataset(archive: zipfile.ZipFile) -> tuple[str, SnapshotSource]:
    names = archive.namelist()
    if any(n.endswith("sr_legacy_food.csv") for n in names):
        return "sr_legacy", "fdc_sr_legacy"
    if any(n.endswith("foundation_food.csv") for n in names):
        return "foundation", "fdc_foundation"
    raise ValueError("not a FoodData Central Foundation or SR Legacy CSV download")


def load_fdc_subset(
    session: Session,
    path: Path,
    *,
    info: SourceInfo | None = None,
    crosswalk: list[CrosswalkRow] | None = None,
) -> LoadResult:
    """Nutrients per 100 g for the crosswalk items that name this dataset (the zip as downloaded)."""
    crosswalk = crosswalk if crosswalk is not None else read_crosswalk()
    with zipfile.ZipFile(path) as archive:
        dataset, source = fdc_dataset(archive)
        wanted = {e.fdc_id: e.slug for e in crosswalk if e.fdc_dataset == dataset}
        foods = {row["fdc_id"] for row in _zip_csv(archive, "food.csv")}
        missing = sorted(set(wanted) - foods)
        if missing:
            raise ValueError(
                f"FDC ids from the crosswalk are not in {path.name}: {missing}"
            )
        nutrients = {row["id"]: row for row in _zip_csv(archive, "nutrient.csv")}
        rows = [
            row
            for row in _zip_csv(archive, "food_nutrient.csv")
            if row["fdc_id"] in wanted and row["amount"] != ""
        ]
    default = (
        sources.FDC_SR_LEGACY if dataset == "sr_legacy" else sources.FDC_FOUNDATION
    )
    snapshot, created = register_snapshot(
        session,
        source=source,
        path=path,
        info=info or default,
        row_count=len(foods),
        crosswalk_sha256=crosswalk_fingerprint(crosswalk),
    )
    if not created:
        return LoadResult(snapshot, False, 0)
    catalog = ensure_catalog(session, crosswalk)
    for row in rows:
        nutrient = nutrients[row["nutrient_id"]]
        session.add(
            CatalogNutrient(
                catalog_item_id=catalog[wanted[row["fdc_id"]]].id,
                nutrient=nutrient["name"],
                amount_per_100g=float(row["amount"]),
                unit=nutrient["unit_name"],
                snapshot_id=snapshot.id,
            )
        )
    session.flush()
    return LoadResult(snapshot, True, len(rows))


# ---------- USDA SR28: household-measure weights and refuse ----------


def _sr28_rows(archive: zipfile.ZipFile, name: str) -> Iterator[list[str]]:
    """Rows of an SR28 ASCII file: fields split on ^, text fields wrapped in ~, Latin-1."""
    if name not in archive.namelist():
        raise ValueError(f"{name} is missing from the download")
    with archive.open(name) as raw:
        for line in io.TextIOWrapper(raw, encoding="latin-1", newline=""):
            line = line.rstrip("\r\n")
            if line:
                yield [field.strip("~") for field in line.split("^")]


def is_sr28(archive: zipfile.ZipFile) -> bool:
    names = set(archive.namelist())
    return {"FOOD_DES.txt", "WEIGHT.txt"} <= names


def purchased_grams(edible_grams: float, refuse_pct: int) -> float:
    """Weight as purchased from edible weight: SR28 refuse is a percent of the weight as bought."""
    return edible_grams / (1 - refuse_pct / 100)


def load_sr28_portions(
    session: Session,
    path: Path,
    *,
    info: SourceInfo = sources.SR28,
    crosswalk: list[CrosswalkRow] | None = None,
    portions: list[PortionMapRow] | None = None,
) -> LoadResult:
    """Weight of one purchased unit for each portion-map row (the SR28 ASCII zip as downloaded).

    SR28 gives household measures as edible weight; the weight as purchased divides out the food's
    refuse percentage (SR28's, or the portion map's override). The source text records the
    measure and the arithmetic.
    """
    crosswalk = crosswalk if crosswalk is not None else read_crosswalk()
    portions = (
        portions if portions is not None else read_portion_map(crosswalk=crosswalk)
    )
    mapped = [row for row in portions if row.has_source]
    with zipfile.ZipFile(path) as archive:
        if not is_sr28(archive):
            raise ValueError("not an SR28 ASCII download (FOOD_DES.txt and WEIGHT.txt)")
        refuse: dict[str, str] = {}
        for fields in _sr28_rows(archive, "FOOD_DES.txt"):
            refuse[fields[0]] = fields[8]
        wanted = {(row.sr28_ndb, row.sr28_seq) for row in mapped}
        weights = {
            (fields[0], fields[1]): fields
            for fields in _sr28_rows(archive, "WEIGHT.txt")
            if (fields[0], fields[1]) in wanted
        }
    missing = sorted(wanted - set(weights))
    if missing:
        raise ValueError(
            f"SR28 weights named in the portion map are not in {path.name}: {missing}"
        )
    no_refuse = sorted({ndb for ndb, _ in wanted if refuse.get(ndb, "") == ""})
    if no_refuse:
        raise ValueError(f"SR28 foods without a refuse value: {no_refuse}")
    snapshot, created = register_snapshot(
        session,
        source="usda_sr28",
        path=path,
        info=info,
        row_count=len(refuse),
        crosswalk_sha256=crosswalk_fingerprint(crosswalk, portions),
    )
    if not created:
        return LoadResult(snapshot, False, 0)
    catalog = ensure_catalog(session, crosswalk)
    for row in mapped:
        _, _, amount, description, gram_weight, *_ = weights[
            (row.sr28_ndb, row.sr28_seq)
        ]
        edible = float(gram_weight) / float(amount)
        sr28_refuse = int(refuse[row.sr28_ndb])
        refuse_pct = (
            row.refuse_override if row.refuse_override is not None else sr28_refuse
        )
        refuse_text = (
            f"{refuse_pct}% refuse"
            if refuse_pct == sr28_refuse
            else f"{refuse_pct}% refuse (owner override; SR28 gives {sr28_refuse}%)"
        )
        session.add(
            CatalogPortion(
                catalog_item_id=catalog[row.slug].id,
                unit=row.count_unit,
                size=row.portion_size,
                grams=purchased_grams(edible, refuse_pct),
                edible_grams=edible,
                refuse_pct=refuse_pct,
                description=description,
                source=(
                    f"USDA SR28 NDB {row.sr28_ndb}, weight {row.sr28_seq}: {amount} "
                    f"{description} = {gram_weight} g edible; {refuse_text}"
                ),
                snapshot_id=snapshot.id,
            )
        )
    session.flush()
    return LoadResult(snapshot, True, len(mapped))


def load_usda_zip(session: Session, path: Path) -> LoadResult:
    """Load a USDA zip by what it contains: SR28 household measures, or a FoodData Central CSV."""
    with zipfile.ZipFile(path) as archive:
        sr28 = is_sr28(archive)
    return load_sr28_portions(session, path) if sr28 else load_fdc_subset(session, path)
