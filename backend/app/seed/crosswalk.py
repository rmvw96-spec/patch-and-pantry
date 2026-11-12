"""The catalog crosswalk: each catalog slug's name in every source. Exact matches only.

``app/seed_data/catalog_crosswalk.csv`` is reviewed by the owner. Loaders never guess a match:
a source row is used only when the crosswalk names it.
"""

import csv
import hashlib
import json
from dataclasses import astuple, dataclass
from pathlib import Path
from typing import cast, get_args

from sqlmodel import Session, select

from app.models import CatalogItem, CatalogKind, CountUnit, PortionSize

SEED_DATA = Path(__file__).resolve().parents[1] / "seed_data"
CROSSWALK_PATH = SEED_DATA / "catalog_crosswalk.csv"
PORTIONS_PATH = SEED_DATA / "catalog_portions.csv"


@dataclass(frozen=True)
class CrosswalkRow:
    slug: str
    name: str
    kind: str
    category: str
    ers_item: str
    ers_form: str
    fdc_id: str
    fdc_dataset: str
    foodkeeper_id: str
    foodkeeper_location: str
    minnesota_crop: str
    planting_chart_crops: tuple[str, ...]
    stand_in_for: str
    notes: str

    @property
    def is_produce(self) -> bool:
        return self.kind == "produce"


def read_crosswalk(path: Path = CROSSWALK_PATH) -> list[CrosswalkRow]:
    with path.open(newline="", encoding="utf-8") as f:
        rows = [
            CrosswalkRow(
                **{
                    key: value
                    for key, value in raw.items()
                    if key != "planting_chart_crops"
                },
                planting_chart_crops=tuple(
                    crop for crop in raw["planting_chart_crops"].split(";") if crop
                ),
            )
            for raw in csv.DictReader(f)
        ]
    slugs = [row.slug for row in rows]
    if len(slugs) != len(set(slugs)):
        raise ValueError(f"duplicate slugs in {path}")
    bad_kinds = {row.kind for row in rows} - set(get_args(CatalogKind))
    if bad_kinds:
        raise ValueError(f"unknown kinds in {path}: {sorted(bad_kinds)}")
    unknown = {row.stand_in_for for row in rows if row.stand_in_for} - set(slugs)
    if unknown:
        raise ValueError(f"stand-ins name unknown slugs: {sorted(unknown)}")
    return rows


def crosswalk_fingerprint(
    crosswalk: list[CrosswalkRow],
    portions: list[PortionMapRow] | None = None,
    loader_version: str = "",
) -> str:
    """SHA-256 of the mapping rows used for a load, independent of row order.

    Stored on each snapshot, so loading a file again through a changed crosswalk (or portion map)
    is a new snapshot that applies the new mapping, while loading it again unchanged is a no-op.
    ``loader_version`` does the same when a loader starts reading more from the same file.
    """
    rows = sorted(astuple(row) for row in crosswalk)
    if portions is not None:
        rows += sorted(astuple(row) for row in portions)
    if loader_version:
        rows.append(("loader_version", loader_version))
    canonical = json.dumps(rows, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PortionMapRow:
    """One count unit of a catalog item and the SR28 household-measure row that weighs it.

    A row without an SR28 reference marks an item sold by count that has no published weight;
    the coverage report lists it. ``refuse_pct_override`` replaces SR28's refuse percentage when
    the owner has judged it wrong for the item as sold (the notes say why).
    """

    slug: str
    unit: str
    size: str
    sr28_ndb: str
    sr28_seq: str
    refuse_pct_override: str
    notes: str

    @property
    def refuse_override(self) -> int | None:
        return int(self.refuse_pct_override) if self.refuse_pct_override else None

    @property
    def has_source(self) -> bool:
        return bool(self.sr28_ndb)

    @property
    def count_unit(self) -> CountUnit:
        return cast(CountUnit, self.unit)  # checked in read_portion_map

    @property
    def portion_size(self) -> PortionSize | None:
        return cast(PortionSize, self.size) if self.size else None


def read_portion_map(
    path: Path = PORTIONS_PATH, crosswalk: list[CrosswalkRow] | None = None
) -> list[PortionMapRow]:
    """Read ``catalog_portions.csv`` (owner-reviewed), rejecting anything a loader would misuse."""
    with path.open(newline="", encoding="utf-8") as f:
        rows = [PortionMapRow(**raw) for raw in csv.DictReader(f)]
    slugs = {
        row.slug for row in (crosswalk if crosswalk is not None else read_crosswalk())
    }
    problems = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        key = (row.slug, row.unit, row.size)
        if row.slug not in slugs:
            problems.append(f"{row.slug}: not in the crosswalk")
        if row.unit not in get_args(CountUnit):
            problems.append(f"{row.slug}: unknown unit {row.unit!r}")
        if row.size and row.size not in get_args(PortionSize):
            problems.append(f"{row.slug}: unknown size {row.size!r}")
        if bool(row.sr28_ndb) != bool(row.sr28_seq):
            problems.append(f"{row.slug}: give both sr28_ndb and sr28_seq, or neither")
        override = row.refuse_pct_override
        if override and not (override.isdigit() and int(override) < 100):
            problems.append(
                f"{row.slug}: refuse_pct_override must be 0 to 99, not {override!r}"
            )
        if override and not row.has_source:
            problems.append(f"{row.slug}: refuse_pct_override needs an SR28 weight")
        if key in seen:
            problems.append(
                f"{row.slug}: {row.unit} {row.size or '(no size)'} is listed twice"
            )
        seen.add(key)
    if problems:
        raise ValueError(f"{path.name}: " + "; ".join(problems))
    return rows


def _initial_conversion_source(row: CrosswalkRow) -> str:
    """Every catalog fact has a source, including why there is none yet."""
    if not row.is_produce:
        return "not applicable: garden supply"
    if row.stand_in_for:
        return f"stand-in: conversions borrowed from {row.stand_in_for}"
    if row.ers_item:
        return "pending: USDA ERS prices not loaded yet"
    return "none: no USDA ERS fresh form; use captured prices"


def ensure_catalog(
    session: Session, crosswalk: list[CrosswalkRow]
) -> dict[str, CatalogItem]:
    """Create any missing national catalog items from the crosswalk; return them by slug."""
    existing = {
        item.slug: item
        for item in session.exec(
            select(CatalogItem).where(CatalogItem.scope == "national")
        )
    }
    for row in crosswalk:
        if row.slug not in existing:
            item = CatalogItem(
                slug=row.slug,
                name=row.name,
                kind=cast(CatalogKind, row.kind),  # validated by read_crosswalk
                category=row.category,
                scope="national",
                conversion_source=_initial_conversion_source(row),
            )
            session.add(item)
            existing[row.slug] = item
    session.flush()
    for row in crosswalk:
        if row.stand_in_for:
            existing[row.slug].stand_in_for_id = existing[row.stand_in_for].id
    session.flush()
    return existing
