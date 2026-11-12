"""Tool ``find_catalog_item``: match typed item text to a catalog item, deterministically (D7).

Three steps, so users never need to know catalog names:

1. Exact match of the folded text (lower case, plurals and filler words removed) against the
   household's confirmed aliases, then catalog names and the reviewed alias list
   (``app/seed_data/catalog_aliases.csv``). An alias listed for several items ("greens") is
   ambiguous on purpose and becomes a question listing them.
2. Fuzzy match against the same names with the standard library's ``difflib``.
3. One candidate at or above ``CUTOFF`` and at least ``MARGIN`` ahead of the next item is a match;
   anything else is a question listing up to five candidates.

No model is involved: the agent passes the result through. Matches are shown on the draft and
saved as household aliases only when the user confirms them (``remember_alias``).
"""

import csv
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import cache
from pathlib import Path
from typing import Literal

from sqlmodel import Session, col, or_, select

from app.models import CatalogItem, Household, HouseholdAlias

ALIASES_PATH = Path(__file__).resolve().parents[1] / "seed_data" / "catalog_aliases.csv"

# A fuzzy match needs this score (difflib ratio, 0 to 1) and this lead over the next item.
# Starting values; the owner's real typed lists and the evals tune them (Phase 1 spec, D7).
CUTOFF = 0.80
MARGIN = 0.10
MAX_CANDIDATES = 5
# Below this score a name is too far off to suggest ("kumquats" is not like "squash").
SUGGEST_FLOOR = 0.6

# Words that describe a purchase, not the item: dropped before matching.
FILLER = frozenset(
    {"fresh", "organic", "local", "loose", "whole", "baby", "small", "medium", "large"}
)
_PLURAL_ES = ("oes", "ches", "shes", "sses", "xes")

MatchKind = Literal["household", "exact", "fuzzy", "ambiguous", "none"]


def _singular(word: str) -> str:
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(_PLURAL_ES):
        return word[:-2]
    if len(word) > 2 and word.endswith("s") and not word.endswith(("ss", "us")):
        return word[:-1]
    return word


def fold(text: str) -> str:
    """The form used for matching: "Yellow Onions, organic" -> "yellow onion"."""
    words = re.findall(r"[a-z]+", text.lower().replace("é", "e").replace("ñ", "n"))
    return " ".join(_singular(w) for w in words if w not in FILLER)


@dataclass(frozen=True)
class Candidate:
    item_id: uuid.UUID
    slug: str
    name: str
    score: float  # 1.0 for an exact match


@dataclass(frozen=True)
class Match:
    kind: MatchKind
    text: str  # as given
    folded: str
    item: Candidate | None = None  # set for household, exact, and fuzzy matches
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def is_match(self) -> bool:
        return self.item is not None

    @property
    def note(self) -> str | None:
        """What the draft shows about the match ("matched 'yellow onoins' to Onion")."""
        if self.item is None:
            return None
        if self.kind == "fuzzy":
            return f"matched '{self.text}' to {self.item.name}"
        if self.kind == "household":
            return f"'{self.text}' is {self.item.name}, as you confirmed before"
        return None


@dataclass(frozen=True)
class AliasRow:
    alias: str
    slug: str
    notes: str


@cache
def _read_aliases(path: Path) -> tuple[AliasRow, ...]:
    with path.open(newline="", encoding="utf-8") as f:
        return tuple(AliasRow(**row) for row in csv.DictReader(f))


def read_aliases(path: Path = ALIASES_PATH) -> tuple[AliasRow, ...]:
    """The reviewed alias list. Cached: it is part of the code, versioned in git."""
    return _read_aliases(path)


def _catalog(session: Session, household: Household | None) -> list[CatalogItem]:
    """Produce items the household can see: national, its region's, and its own."""
    scopes = [col(CatalogItem.scope) == "national"]
    if household is not None:
        scopes.append(col(CatalogItem.region_id) == household.region_id)
        scopes.append(col(CatalogItem.household_id) == household.id)
    return list(
        session.exec(
            select(CatalogItem).where(CatalogItem.kind == "produce", or_(*scopes))
        )
    )


def _candidate(item: CatalogItem, score: float) -> Candidate:
    return Candidate(item.id, item.slug, item.name, round(score, 4))


def find_catalog_item(
    session: Session,
    text: str,
    household: Household | None = None,
    aliases: tuple[AliasRow, ...] | None = None,
) -> Match:
    """Match one typed item name. Deterministic: the same text and data give the same answer."""
    folded = fold(text)
    if not folded:
        return Match("none", text, folded)
    items = _catalog(session, household)
    by_id = {item.id: item for item in items}
    by_slug = {item.slug: item for item in items}

    # 1. The household's own confirmed name wins.
    if household is not None:
        mine = session.exec(
            select(HouseholdAlias).where(
                HouseholdAlias.household_id == household.id,
                HouseholdAlias.text == folded,
            )
        ).first()
        if mine is not None and mine.catalog_item_id in by_id:
            mine_item = _candidate(by_id[mine.catalog_item_id], 1.0)
            return Match("household", text, folded, mine_item, [mine_item])

    # Every name each item answers to: its catalog name and its aliases, folded.
    names: dict[str, set[uuid.UUID]] = defaultdict(set)
    for catalog_item in items:
        names[fold(catalog_item.name)].add(catalog_item.id)
    for row in aliases if aliases is not None else read_aliases():
        if row.slug in by_slug:
            names[fold(row.alias)].add(by_slug[row.slug].id)

    # 2. Exact: one item is a match; an alias shared by several items is a question.
    exact = names.get(folded, set())
    if len(exact) == 1:
        only = _candidate(by_id[next(iter(exact))], 1.0)
        return Match("exact", text, folded, only, [only])
    if exact:
        ranked = sorted(
            (_candidate(by_id[i], 1.0) for i in exact), key=lambda c: c.name
        )
        return Match("ambiguous", text, folded, None, ranked)

    # 3. Fuzzy: each item's best score over all its names.
    best: dict[uuid.UUID, float] = {}
    for name, ids in names.items():
        score = SequenceMatcher(None, folded, name).ratio()
        for item_id in ids:
            best[item_id] = max(best.get(item_id, 0.0), score)
    ranked = sorted(
        (_candidate(by_id[i], s) for i, s in best.items()),
        key=lambda c: (-c.score, c.name),
    )[:MAX_CANDIDATES]
    if not ranked:
        return Match("none", text, folded)
    top = ranked[0]
    runner_up = ranked[1].score if len(ranked) > 1 else 0.0
    if top.score >= CUTOFF and top.score - runner_up >= MARGIN:
        return Match("fuzzy", text, folded, top, ranked)
    suggestions = [c for c in ranked if c.score >= SUGGEST_FLOOR]
    return Match("none", text, folded, None, suggestions)


def remember_alias(
    session: Session, household: Household, text: str, item: CatalogItem
) -> HouseholdAlias:
    """Save a confirmed match, so the same text matches exactly next time. Flushes, no commit."""
    folded = fold(text)
    if not folded:
        raise ValueError("an alias needs at least one word")
    existing = session.exec(
        select(HouseholdAlias).where(
            HouseholdAlias.household_id == household.id, HouseholdAlias.text == folded
        )
    ).first()
    alias = existing or HouseholdAlias(
        household_id=household.id, text=folded, catalog_item_id=item.id
    )
    alias.catalog_item_id = item.id
    session.add(alias)
    session.flush()
    return alias
