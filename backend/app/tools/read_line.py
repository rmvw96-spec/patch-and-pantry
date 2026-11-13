"""Reading one typed price line with rules: item words, quantity, unit, size, and price.

Typed lists are read here first; only the lines these rules are not sure of go to the model
(owner decision, 2026-11-13: DeepSeek-V3.2 split prices and units differently from run to run).
Like the model, the rules copy what is written and never calculate: the capture code does the
math. The demo model uses the same rules, so demo mode reads lists the same way.
"""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# ---------- prices ----------

_PRICE = re.compile(r"^\$?\s*(\d+(?:\.\d{1,2})?|\.\d{1,2})\s*$")
_CENTS = re.compile(r"^(\d{1,2})\s*(?:c|¢|cents?)$")


def price_cents(text: str | None) -> int | None:
    """'2.99', '$5', '.99', '99c' -> integer cents; None if the text is not a plain price."""
    if text is None:
        return None
    cleaned = text.strip().lower().replace(",", "")
    cents = _CENTS.match(cleaned)
    if cents:
        return int(cents.group(1))
    dollars = _PRICE.match(cleaned)
    if not dollars:
        return None
    try:
        return int((Decimal(dollars.group(1)) * 100).to_integral_value())
    except InvalidOperation:  # pragma: no cover - the pattern admits only valid numbers
        return None


# A unit written inside the price: "$3/lb", "99 cents a pound", "$20 per box", "1.29 ea". The
# model sometimes copies the price that way and leaves the unit empty (2 of 6 DeepSeek runs on a
# real list, 2026-11-13).
_PER = r"/|\bper\b|\ban?\b"
_BARE_UNITS = r"each|ea|lbs?|oz"
_PRICE_UNIT_TAIL = rf"(?:\s*(?:{_PER})\s*[a-z][a-z.]*|\s+(?:{_BARE_UNITS})\b)"
_PRICE_PER_UNIT = re.compile(
    rf"^(?P<amount>.+?)\s*(?:(?:{_PER})\s*(?P<unit>[a-z][a-z.]*)|\b(?P<bare>{_BARE_UNITS}))$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WrittenPrice:
    """A price as written, split into its amount and the unit it is for, if one is written."""

    cents: int | None
    amount: str | None = None  # the price words alone: "$3" from "$3/lb"
    per: str | None = None  # "lb" from "$3/lb"; None for a price of the whole amount


def read_price(text: str | None) -> WrittenPrice:
    """'$3/lb' -> 300 per 'lb'; '$5' -> 500 for the whole amount; 'two' -> no price."""
    cents = price_cents(text)
    if text is None or cents is not None:
        return WrittenPrice(cents, text.strip() if text else None)
    split = _PRICE_PER_UNIT.match(text.strip())
    if split is None:
        return WrittenPrice(None)
    cents = price_cents(split.group("amount"))
    if cents is None:
        return WrittenPrice(None)
    per = (split.group("unit") or split.group("bare")).lower().rstrip(".")
    return WrittenPrice(cents, split.group("amount").strip(), per)


# ---------- one line ----------

# A price at the end of the line, with the unit it is for if one is written ("$3/lb",
# "69 cents a pound"); read_price splits the two.
_PRICE_AT_END = re.compile(
    rf"(?P<price>\$?\s*\d*\.?\d+(?:\s*(?:c\b|¢|cents?\b))?{_PRICE_UNIT_TAIL}?)\s*$",
    re.IGNORECASE,
)
# "@" or "at" just before the price makes it a price for one unit: "3 lb @ 1.49" is $1.49 a lb.
_AT = re.compile(r"(?:@|\bat)\s*$", re.IGNORECASE)
_FOR = re.compile(r"\b(?P<qty>\d+)\s*(?:for|/)\s*$", re.IGNORECASE)
_QTY_UNIT = re.compile(
    r"\b(?P<qty>\d+(?:\.\d+)?)?\s*(?P<unit>lbs?|pounds?|oz|ounces?|kg|each|ea|bunch(?:es)?|heads?|ears?|pints?)\b",
    re.IGNORECASE,
)
# A count written after the price: "(3)", "x3", "- 3".
_COUNT_AT_END = re.compile(
    r"\s*(?:\((?P<a>\d+)\)|x\s*(?P<b>\d+)|-\s*(?P<c>\d+))\s*$", re.IGNORECASE
)
_SIZE = re.compile(r"\b(small|medium|large)\b", re.IGNORECASE)
_PACKAGING = re.compile(r"\b(bag|box|pack|package|bundle)\b", re.IGNORECASE)
_EMPTY_BRACKETS = re.compile(r"\(\s*\)")  # left by a size taken out: "Amish (medium)"
_LEADING_OF = re.compile(
    r"^of\s+", re.IGNORECASE
)  # a unit before the item: "Head of lettuce"
# Words left joining the item to what was taken out: "Blueberries 2 pints for $7".
_TRAILING_JOINER = re.compile(r"(?:\s+(?:for|at|or|and|&))+$", re.IGNORECASE)
_EDGE_PUNCTUATION = " ,;:-–@"


def _item_words(rest: str) -> str | None:
    """What is left once the amount, unit, size, and price are out: the item words."""
    item = " ".join(_EMPTY_BRACKETS.sub("", _PACKAGING.sub("", rest)).split())
    # Separators left at the ends: "Tomatoes - 3 lbs - $4.50", "Kale, 1 bunch, $2.50".
    item = _TRAILING_JOINER.sub("", item.strip(_EDGE_PUNCTUATION))
    item = _LEADING_OF.sub("", item).strip(_EDGE_PUNCTUATION)
    return item or None


def read_list_line(text: str) -> dict[str, object]:
    """Read one typed line into the fields the model would give (``ExtractedLine``)."""
    entry: dict[str, object] = {"is_price": False}
    count = _COUNT_AT_END.search(text)
    if count:
        text = text[: count.start()]
    price = _PRICE_AT_END.search(text)
    if not price:
        return entry
    rest = text[: price.start()].rstrip()
    written = read_price(price.group("price"))
    entry |= {
        "is_price": True,
        "price_text": written.amount,
        "price_is_per_unit": written.per is not None,
    }
    if written.per:
        entry["unit"] = written.per
    at = _AT.search(rest)
    if at:
        entry["price_is_per_unit"] = True
        rest = rest[: at.start()]
    lot = _FOR.search(rest)
    if lot:
        entry |= {"quantity": float(lot.group("qty")), "unit": "each"}
        rest = rest[: lot.start()]
    unit = _QTY_UNIT.search(rest)
    if unit:
        if unit.group("qty"):
            entry["quantity"] = float(unit.group("qty"))
        entry.setdefault("unit", unit.group("unit").lower())
        rest = rest[: unit.start()] + rest[unit.end() :]
    size = _SIZE.search(rest)
    if size:
        entry["size"] = size.group(1).lower()
        rest = rest[: size.start()] + rest[size.end() :]
    if count:
        entry["quantity"] = float(
            count.group("a") or count.group("b") or count.group("c")
        )
    entry["item_text"] = _item_words(rest)
    return entry


_UNREAD_PRICE = re.compile(r"\d|\$|dollar|cent", re.IGNORECASE)
_LEFTOVER = re.compile(r"\d|\$|#")
# A whole number with no "$", cents, or decimal point may not be a price: "Kroger #42".
_BARE_NUMBER = re.compile(r"^\d+$")


def needs_model(text: str, entry: dict[str, object]) -> bool:
    """True when the rules are not sure of their reading, so the model should read the line.

    Sure: a price read with plain item words, or a line with no number or money word at all
    (a store name or a note). Not sure: a number or money word but no price read ("$3 tomatoes",
    "two for five dollars"), no item words, or numbers or symbols left in the item words
    ("1/2 bushel $20 or $2/lb", "Tomatoes 3# $5"), or a price that is a bare whole number
    ("Kroger #42", "Watermelon 5").
    """
    if not entry["is_price"]:
        return bool(_UNREAD_PRICE.search(text))
    item = entry.get("item_text")
    price = entry.get("price_text")
    return (
        not isinstance(item, str)
        or bool(_LEFTOVER.search(item))
        or (isinstance(price, str) and bool(_BARE_NUMBER.match(price)))
    )
