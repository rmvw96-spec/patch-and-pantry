"""Tool find_catalog_item (Phase 1, P1-5 and D7): exact, alias, fuzzy, ambiguous, household."""

from pathlib import Path

import pytest
from sqlmodel import Session, func, select

from app.models import CatalogItem, Household, HouseholdAlias
from app.tools.catalog import (
    CUTOFF,
    MARGIN,
    AliasRow,
    find_catalog_item,
    fold,
    read_aliases,
    remember_alias,
)
from tests.catalog_fixtures import make_household

Catalog = dict[str, CatalogItem]
# Every test runs against the known test catalog (tests/catalog_fixtures.py).
pytestmark = pytest.mark.usefixtures("catalog")


@pytest.mark.parametrize(
    ("text", "folded"),
    [
        ("Onions", "onion"), ("Yellow Onions", "yellow onion"), ("TOMATOES", "tomato"),
        ("radishes", "radish"), ("peaches", "peach"), ("berries", "berry"),
        ("Swiss chard", "swiss chard"), ("asparagus", "asparagus"),
        ("organic baby spinach", "spinach"), ("large tomatoes, fresh!", "tomato"),
        ("jalapeños", "jalapeno"), ("black-eyed peas", "black eyed pea"), ("3 lb", "lb"),
        ("", ""), ("$2.99", ""),
    ],
)  # fmt: skip
def test_fold(text: str, folded: str) -> None:
    assert fold(text) == folded


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        ("onions", "onion"), ("Onion", "onion"), ("yellow onions", "onion"),
        ("russets", "potato"), ("slicing tomatoes", "tomato"), ("collard greens", "collards"),
        ("romaine", "lettuce"), ("corn", "sweet-corn"), ("bell peppers", "sweet-pepper"),
        ("jalapeños", "hot-pepper"), ("organic kale", "kale"),
    ],
)  # fmt: skip
def test_exact_names_and_aliases(db_session: Session, text: str, slug: str) -> None:
    match = find_catalog_item(db_session, text)
    assert match.kind == "exact"
    assert match.item is not None and match.item.slug == slug
    assert match.note is None  # nothing to point out for an exact name


@pytest.mark.parametrize(
    ("text", "slug"),
    [("yellow onoins", "onion"), ("brocoli", "broccoli"), ("tomatoe", "tomato"), ("zuchini", "zucchini")],  # spellchecker:disable-line
)  # fmt: skip
def test_misspellings_match_and_say_so(
    db_session: Session, text: str, slug: str
) -> None:
    match = find_catalog_item(db_session, text)
    assert match.kind == "fuzzy"
    assert match.item is not None and match.item.slug == slug
    assert match.item.score >= CUTOFF
    assert match.note == f"matched '{text}' to {match.item.name}"


@pytest.mark.parametrize(
    ("text", "names"),
    [
        (
            "greens",
            ["Collards", "Kale", "Mustard greens", "Swiss chard", "Turnip greens"],
        ),
        ("squash", ["Acorn squash", "Butternut squash", "Yellow squash", "Zucchini"]),
        ("peppers", ["Hot pepper", "Sweet pepper"]),
    ],
)
def test_shared_aliases_are_questions(
    db_session: Session, text: str, names: list[str]
) -> None:
    match = find_catalog_item(db_session, text)
    assert match.kind == "ambiguous" and match.item is None
    assert [c.name for c in match.candidates] == names


def test_unknown_item_suggests_only_close_names(db_session: Session) -> None:
    assert find_catalog_item(db_session, "kumquats").candidates == []
    nearly = find_catalog_item(db_session, "zucc")
    assert nearly.kind == "none"
    assert nearly.candidates[0].slug == "zucchini"
    assert len(find_catalog_item(db_session, "e").candidates) <= 5


def test_a_close_second_is_a_question_not_a_match(db_session: Session) -> None:
    """Two items nearly as good as each other: the margin turns a match into a question."""
    aliases = (AliasRow("cabbage", "kale", ""), AliasRow("cabbagf", "collards", ""))
    match = find_catalog_item(db_session, "cabbagg", aliases=aliases)
    assert match.kind == "none"
    top, second = match.candidates[:2]
    assert top.score >= CUTOFF and top.score - second.score < MARGIN


def test_empty_text_matches_nothing(db_session: Session) -> None:
    assert find_catalog_item(db_session, "  ").kind == "none"


def test_household_alias_wins_and_belongs_to_one_household(
    db_session: Session, catalog: Catalog, household: Household
) -> None:
    remember_alias(db_session, household, "Greens", catalog["kale"])

    mine = find_catalog_item(db_session, "greens", household)
    assert mine.kind == "household" and mine.item is not None
    assert mine.item.slug == "kale"
    assert mine.note == "'greens' is Kale, as you confirmed before"
    neighbor = make_household(db_session)
    assert find_catalog_item(db_session, "greens", neighbor).kind == "ambiguous"


def test_remembering_again_updates_the_alias(
    db_session: Session, catalog: Catalog, household: Household
) -> None:
    remember_alias(db_session, household, "greens", catalog["kale"])
    remember_alias(db_session, household, "Greens!", catalog["collards"])

    count = db_session.exec(
        select(func.count())
        .select_from(HouseholdAlias)
        .where(HouseholdAlias.household_id == household.id)
    ).one()
    assert count == 1
    match = find_catalog_item(db_session, "greens", household)
    assert match.item is not None and match.item.slug == "collards"
    with pytest.raises(ValueError, match="at least one word"):
        remember_alias(db_session, household, "123", catalog["kale"])


def test_household_items_are_matched_only_for_that_household(
    db_session: Session, household: Household
) -> None:
    own = CatalogItem(
        slug="pawpaw",
        name="Pawpaw",
        kind="produce",
        category="fruit",
        scope="household",
        household_id=household.id,
        conversion_source="owner",
    )
    db_session.add(own)
    db_session.flush()
    assert find_catalog_item(db_session, "pawpaws", household).kind == "exact"
    assert find_catalog_item(db_session, "pawpaws").kind == "none"


def test_supplies_are_never_matched(db_session: Session) -> None:
    db_session.add(
        CatalogItem(
            slug="compost-test",
            name="Compost",
            kind="supply",
            category="soil",
            conversion_source="test",
        )
    )
    db_session.flush()
    assert find_catalog_item(db_session, "compost").kind == "none"


def test_the_alias_list_is_valid() -> None:
    """Every alias names a crosswalk produce slug; a single-item alias is not listed twice."""
    from app.seed.crosswalk import read_crosswalk

    produce = {row.slug for row in read_crosswalk() if row.is_produce}
    rows = read_aliases()
    assert {row.slug for row in rows} <= produce
    pairs = [(fold(row.alias), row.slug) for row in rows]
    assert len(pairs) == len(set(pairs))
    names = {fold(row.name) for row in read_crosswalk()}
    repeats = names & {fold(row.alias) for row in rows}
    assert not repeats, f"aliases repeat catalog names: {repeats}"


def test_aliases_are_read_once(tmp_path: Path) -> None:
    path = tmp_path / "aliases.csv"
    path.write_text("alias,slug,notes\nspud,potato,\n", encoding="utf-8")
    first = read_aliases(path)
    path.write_text("alias,slug,notes\n", encoding="utf-8")
    assert read_aliases(path) is first
