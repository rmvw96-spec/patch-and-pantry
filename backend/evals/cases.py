"""Eval cases for typed price lists, loaded from ``evals/cases/*.yaml``.

A case is a short typed list and what should happen to each line once it is read and matched to
the catalog: a price (with its item, amount, unit, and total), a question of a given kind, or a
note set aside. A line may list several acceptable outcomes.
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents.capture_agent import QuestionKind

CASES_DIR = Path(__file__).parent / "cases"

# How a case passes (spec P1-9, D2): extraction cases need at least 95% of their fields right in
# every run; clarification and behavior cases need every field right in every run.
Group = Literal["extraction", "clarification", "behavior"]
GROUPS: tuple[Group, ...] = ("extraction", "clarification", "behavior")
EXTRACTION_THRESHOLD = 0.95


class Expect(BaseModel):
    """One acceptable outcome for a line. Fields left out are not scored."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    note: bool = False  # set aside as not a price (a store name, a note, a question)
    ask: QuestionKind | None = None  # a question of this kind
    options: tuple[str, ...] = ()  # the question offers at least these items
    item: str | None = None  # the catalog item's name, for a price
    quantity: float | None = None
    unit: str | None = None
    price: int | None = None  # cents, for the whole amount bought
    size: str | None = None

    @model_validator(mode="after")
    def _one_outcome(self) -> Expect:
        priced = any(
            v is not None for v in (self.quantity, self.unit, self.price, self.size)
        )
        if self.note and (self.ask or self.item or priced or self.options):
            raise ValueError("a note has no item, price, or question")
        if self.ask and (priced or self.item):
            # Questions about a price or unit do not carry the matched item; score the
            # question's kind and the items it offers.
            raise ValueError("a question has no item, amount, or price; use options")
        if not (self.note or self.ask or (self.item and self.price is not None)):
            raise ValueError("expect a note, a question, or an item with a price")
        return self


class Line(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    expect: tuple[Expect, ...]  # acceptable outcomes; the closest one is scored
    why: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _one_or_many(cls, data: object) -> object:
        if isinstance(data, dict) and isinstance(data.get("expect"), dict):
            return {**data, "expect": [data["expect"]]}
        return data


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    group: Group
    why: str  # the problem this case watches for
    source: str  # where the lines came from: the owner's list, or written for the eval
    lines: tuple[Line, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _distinct_lines(self) -> Case:
        texts = [line.text for line in self.lines]
        if len(set(texts)) != len(texts):
            raise ValueError(f"{self.name}: a line appears twice")
        if any(not t or t != t.strip() or "\n" in t for t in texts):
            raise ValueError(
                f"{self.name}: each line is one line of text, with no spaces at the ends"
            )
        return self

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


class CaseFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: list[Case]


def load_cases(directory: Path = CASES_DIR) -> list[Case]:
    """Every case in the directory's YAML files, in file then case order. Names are unique."""
    cases: list[Case] = []
    for path in sorted(directory.glob("*.yaml")):
        cases += CaseFile.model_validate(yaml.safe_load(path.read_text())).cases
    names = [c.name for c in cases]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError(f"case names used twice: {', '.join(duplicates)}")
    return cases
