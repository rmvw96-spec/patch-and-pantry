"""Coverage report: first-crop-set items lacking a price, conversion, nutrients, storage, or portion.

The expected gaps are listed in docs/design.md ("ERS coverage of the first crop set"); items
without an ERS fresh form rely on captured prices instead. "portion" is checked only for items
the portion map lists as sold by count (app/seed_data/catalog_portions.csv).
"""

from dataclasses import dataclass, field

from sqlmodel import Session, col, func, select

from app.models import CatalogItem, CatalogNutrient, CatalogPortion, PriceObservation
from app.seed.crosswalk import (
    CrosswalkRow,
    PortionMapRow,
    read_crosswalk,
    read_portion_map,
)

CHECKS = ("price", "conversion", "nutrients", "storage", "portion")


@dataclass
class Coverage:
    slug: str
    missing: list[str] = field(default_factory=list)


def _has_conversion(item: CatalogItem, by_id: dict[object, CatalogItem]) -> bool:
    """Conversions are present on the item itself, or on the item it stands in for."""
    if item.preparation_yield_factor is not None and item.cup_equivalent_lb is not None:
        return True
    stand_in = by_id.get(item.stand_in_for_id) if item.stand_in_for_id else None
    return stand_in is not None and _has_conversion(stand_in, by_id)


def coverage_gaps(
    session: Session,
    crosswalk: list[CrosswalkRow] | None = None,
    portions: list[PortionMapRow] | None = None,
) -> list[Coverage]:
    """One entry per first-crop-set produce item that is missing anything (or not loaded at all)."""
    crosswalk = crosswalk if crosswalk is not None else read_crosswalk()
    portions = (
        portions if portions is not None else read_portion_map(crosswalk=crosswalk)
    )
    sold_by_count = {row.slug for row in portions}
    items = list(
        session.exec(select(CatalogItem).where(CatalogItem.scope == "national"))
    )
    by_slug = {item.slug: item for item in items}
    by_id: dict[object, CatalogItem] = {item.id: item for item in items}
    priced = set(
        session.exec(
            select(PriceObservation.catalog_item_id).where(
                col(PriceObservation.household_id).is_(None)
            )
        )
    )
    with_nutrients = set(
        session.exec(
            select(CatalogNutrient.catalog_item_id)
            .group_by(col(CatalogNutrient.catalog_item_id))
            .having(func.count() > 0)
        )
    )
    with_portions = set(session.exec(select(CatalogPortion.catalog_item_id).distinct()))
    gaps = []
    for entry in crosswalk:
        if not entry.is_produce:
            continue
        item = by_slug.get(entry.slug)
        if item is None:
            gaps.append(Coverage(entry.slug, ["not loaded"]))
            continue
        present = {
            "price": item.id in priced,
            "conversion": _has_conversion(item, by_id),
            "nutrients": item.id in with_nutrients,
            "storage": item.storage_days_min is not None,
            "portion": entry.slug not in sold_by_count or item.id in with_portions,
        }
        missing = [check for check in CHECKS if not present[check]]
        if missing:
            gaps.append(Coverage(entry.slug, missing))
    return gaps


def format_report(gaps: list[Coverage]) -> str:
    if not gaps:
        return (
            "Every first-crop-set item has a price, conversion, nutrients, storage time, "
            "and (if sold by count) a portion weight."
        )
    width = max(len(gap.slug) for gap in gaps)
    lines = [f"{len(gaps)} first-crop-set items have gaps:"]
    lines += [
        f"  {gap.slug:<{width}}  missing {', '.join(gap.missing)}" for gap in gaps
    ]
    return "\n".join(lines)
