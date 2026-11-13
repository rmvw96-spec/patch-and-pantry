"""Tool ``find_catalog_item``: match typed item text to a catalog item, deterministically (D7).

Three steps, so users never need to know catalog names:

1. Exact match of the folded text (lower case, plurals and possessives removed) against the
   household's confirmed aliases, then catalog names and the reviewed alias list
   (``app/seed_data/catalog_aliases.csv``). Word order does not matter ("lima beans frozen" is
   "Frozen lima beans"). Filler words ("fresh", "baby") are kept when an item's own name has
   them ("Baby carrots") and dropped otherwise ("baby spinach" is Spinach). An alias listed for
   several items ("greens") is ambiguous on purpose and becomes a question listing them.
2. Fuzzy match against the same names with the standard library's ``difflib``. One candidate at
   or above ``CUTOFF`` and at least ``MARGIN`` ahead of the next item is a match.
3. Broader: drop describing words before the last one ("heirloom tomatoes" is Tomato, "purple
   onions" is Onion). Form words ("frozen", "canned") are never dropped: "butter beans frozen"
   becomes Frozen lima beans through the alias "butter bean", never fresh lima beans. A broader
   match is shown and confirmed like a fuzzy one.
4. Anything else is a question listing up to five candidates.

No model is involved: the agent passes the result through. Matches are shown on the draft and
saved as household aliases only when the user confirms them (``remember_alias``).
"""

import csv
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from functools import cache
from itertools import combinations
from pathlib import Path
from typing import Literal, cast, get_args

from sqlmodel import Session, col, or_, select

from app.models import CatalogItem, Household, HouseholdAlias, PortionSize

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
# Words naming how produce is sold. Never dropped when broadening a name, so a frozen item never
# matches the fresh one (dogfooding: "Lima beans frozen" was offered fresh lima beans).
FORM_WORDS = frozenset({"frozen", "canned", "dried", "juice", "concentrate"})
# Broadening drops at most this many describing words, so the combinations stay few.
MAX_DROPPED = 3
_POSSESSIVE = re.compile(r"['\u2019]s\b")

MatchKind = Literal["household", "exact", "fuzzy", "ambiguous", "none"]


def _singular(word: str) -> str:
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(_PLURAL_ES):
        return word[:-2]
    if len(word) > 2 and word.endswith("s") and not word.endswith(("ss", "us")):
        return word[:-1]
    return word


def _words(text: str, keep_filler: bool = False) -> list[str]:
    cleaned = _POSSESSIVE.sub("", text.lower()).replace("é", "e").replace("ñ", "n")
    words = re.findall(r"[a-z]+", cleaned)
    return [_singular(w) for w in words if keep_filler or w not in FILLER]


def fold(text: str) -> str:
    """The form used for matching: "Yellow Onions, organic" -> "yellow onion"."""
    return " ".join(_words(text))


def _key(words: list[str] | tuple[str, ...]) -> str:
    """Order does not matter: "lima bean frozen" and "frozen lima bean" share a key."""
    return " ".join(sorted(words))


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
    # A size the alias implies ("satsuma" is a large orange); a size on the line wins.
    size: PortionSize | None = None

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
        if self.size is not None:
            return f"'{self.text}' is priced as a {self.size} {self.item.name.lower()}"
        return None


@dataclass(frozen=True)
class AliasRow:
    alias: str
    slug: str
    notes: str
    # Optional: the piece size the alias stands for ("satsuma" is a large orange).
    size: str = ""


@cache
def _read_aliases(path: Path) -> tuple[AliasRow, ...]:
    with path.open(newline="", encoding="utf-8") as f:
        return tuple(
            AliasRow(row["alias"], row["slug"], row["notes"], row.get("size") or "")
            for row in csv.DictReader(f)
        )


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

    # Every name each item answers to: its catalog name and its aliases, folded. ``names`` drops
    # filler words; ``full`` keeps them, so "baby carrots" can tell Baby carrots from Carrot.
    names: dict[str, set[uuid.UUID]] = defaultdict(set)
    full: dict[str, set[uuid.UUID]] = defaultdict(set)
    spoken = [(catalog_item.name, catalog_item.id) for catalog_item in items]
    sized: dict[tuple[str, uuid.UUID], PortionSize] = {}
    for row in aliases if aliases is not None else read_aliases():
        if row.slug in by_slug:
            spoken.append((row.alias, by_slug[row.slug].id))
            if row.size in get_args(PortionSize):
                sized[_key(_words(row.alias)), by_slug[row.slug].id] = cast(
                    PortionSize, row.size
                )
    for name, item_id in spoken:
        names[_key(_words(name))].add(item_id)
        full[_key(_words(name, keep_filler=True))].add(item_id)

    # 2. Exact: one item is a match; an alias shared by several items is a question.
    exact = full.get(_key(_words(text, keep_filler=True))) or names.get(
        _key(_words(text)), set()
    )
    if exact:
        match = _one_or_question("exact", text, folded, exact, by_id)
        if match.item is not None:
            size = sized.get((_key(_words(text)), match.item.item_id))
            return replace(match, size=size)
        return match

    # 3. Broader: the same name without some describing words. Before fuzzy, so a different
    # describing word is not taken for a typo ("green tomato" is not Grape tomato).
    broader, named_base = _broader(folded.split(), names, by_id)
    if len(broader) == 1:
        return _one_or_question("fuzzy", text, folded, broader, by_id)

    # 4. Fuzzy: each item's best score over all its names, in either word order.
    ranked = _rank(folded, spoken, by_id)
    if not ranked:
        return Match("none", text, folded)
    top = _typo_match(folded, ranked, by_id)
    # A broader name shared by several items ("delicate squash") is a question, unless the
    # typo match is one of them (a misspelled "yellow squash" is Yellow squash).
    if top is not None and (not broader or top.item_id in broader):
        return Match("fuzzy", text, folded, top, ranked)
    if broader:
        return _one_or_question("fuzzy", text, folded, broader, by_id)

    # 5. The item word alone, with its forms, for a misspelled item after describing words
    # (dogfooding: "bubblegum plumbs" is Plum).
    words = folded.split()
    rest = [w for w in words if w not in FORM_WORDS]
    # Not when the words already named an item that is not sold in that form ("purple hull
    # peas frozen" is Southern pea, so it is never Frozen peas).
    if len(rest) > 1 and not named_base:
        item_word = " ".join([*(w for w in words if w in FORM_WORDS), rest[-1]])
        head = _typo_match(item_word, _rank(item_word, spoken, by_id), by_id)
        if head is not None:
            return Match("fuzzy", text, folded, head, [head])
    suggestions = [c for c in ranked if c.score >= SUGGEST_FLOOR]
    return Match("none", text, folded, None, suggestions)


def _rank(
    folded: str,
    spoken: list[tuple[str, uuid.UUID]],
    by_id: dict[uuid.UUID, CatalogItem],
) -> list[Candidate]:
    """Items by their best typo score over all their names, in either word order."""
    best: dict[uuid.UUID, float] = {}
    for name, item_id in spoken:
        words = _words(name)
        score = max(
            SequenceMatcher(None, folded, " ".join(words)).ratio(),
            SequenceMatcher(None, _key(folded.split()), _key(words)).ratio(),
        )
        best[item_id] = max(best.get(item_id, 0.0), score)
    return sorted(
        (_candidate(by_id[i], s) for i, s in best.items()),
        key=lambda c: (-c.score, c.name),
    )[:MAX_CANDIDATES]


def _typo_match(
    folded: str, ranked: list[Candidate], by_id: dict[uuid.UUID, CatalogItem]
) -> Candidate | None:
    """The top candidate if it is close enough, clearly ahead, and in the same form."""
    if not ranked:
        return None
    top = ranked[0]
    runner_up = ranked[1].score if len(ranked) > 1 else 0.0
    same_form = _forms(folded.split()) == _forms(_words(by_id[top.item_id].name))
    if same_form and top.score >= CUTOFF and top.score - runner_up >= MARGIN:
        return top
    return None


def _in_form(
    forms: list[str], base: CatalogItem, by_id: dict[uuid.UUID, CatalogItem]
) -> set[uuid.UUID]:
    """The base item sold in these forms: Lima bean + frozen -> Frozen lima beans; Peach +
    canned -> both canned peach items (in juice, in syrup), which become a question."""
    wanted = {*forms, *_words(base.name)}
    named = {i: set(_words(item.name)) for i, item in by_id.items()}
    exact = {i for i, words in named.items() if words == wanted}
    return exact or {
        i
        for i, words in named.items()
        if wanted <= words and _forms(list(words)) >= frozenset(forms)
    }


def _forms(words: list[str]) -> frozenset[str]:
    return frozenset(words) & FORM_WORDS


def _one_or_question(
    kind: MatchKind,
    text: str,
    folded: str,
    ids: set[uuid.UUID],
    by_id: dict[uuid.UUID, CatalogItem],
) -> Match:
    """One item is a match of ``kind``; several are a question listing them."""
    if len(ids) == 1:
        only = _candidate(by_id[next(iter(ids))], 1.0)
        return Match(kind, text, folded, only, [only])
    ranked = sorted((_candidate(by_id[i], 1.0) for i in ids), key=lambda c: c.name)
    return Match("ambiguous", text, folded, None, ranked)


def _broader(
    words: list[str],
    names: dict[str, set[uuid.UUID]],
    by_id: dict[uuid.UUID, CatalogItem],
) -> tuple[set[uuid.UUID], bool]:
    """Items named by the words with the fewest describing words dropped, and whether a base
    item was named (even one not sold in the line's form).

    The last word that is not a form word is the item ("heirloom tomato"), so it is kept, and
    so is every form word. With a form word, a name that matches without it leads to the item
    in that form: "butter bean frozen" -> Lima bean (alias) -> Frozen lima beans.
    """
    forms = [w for w in words if w in FORM_WORDS]
    rest = [w for w in words if w not in FORM_WORDS]
    if not rest:
        return set(), False
    describing, head = rest[:-1], rest[-1]
    for dropped in range(0, min(len(describing), MAX_DROPPED) + 1):
        found: set[uuid.UUID] = set()
        bases: set[uuid.UUID] = set()
        for kept in combinations(describing, len(describing) - dropped):
            base = [*kept, head]
            if not forms:
                # With nothing dropped, the exact step already looked.
                found |= names.get(_key(base), set()) if dropped else set()
                continue
            found |= names.get(_key([*forms, *base]), set())
            for base_id in names.get(_key(base), set()):
                bases.add(base_id)
                found |= _in_form(forms, by_id[base_id], by_id)
        # An item named but not sold in that form stops here: "purple hull peas frozen" is
        # Southern pea, and there are no frozen southern peas, so it is not Frozen peas.
        if found or bases:
            return found, bool(bases)
    return set(), False


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
