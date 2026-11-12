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

from app.models import CatalogItem, CatalogKind

SEED_DATA = Path(__file__).resolve().parents[1] / "seed_data"
CROSSWALK_PATH = SEED_DATA / "catalog_crosswalk.csv"


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


def crosswalk_fingerprint(crosswalk: list[CrosswalkRow]) -> str:
    """SHA-256 of the crosswalk rows used for a load, independent of row order.

    Stored on each snapshot, so loading a file again through a changed crosswalk is a new
    snapshot that applies the new mapping, while loading it again unchanged is a no-op.
    """
    canonical = json.dumps(
        sorted(astuple(row) for row in crosswalk), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
