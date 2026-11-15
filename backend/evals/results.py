"""The results file the runner writes (``--json``) and the report tools read.

One entry per case and reader, keyed ``"name [reader]"``. Each holds the scores over all runs
and every line that was wrong in any run, so a report can be built, or two runs compared,
from the file alone.
"""

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class Miss(BaseModel):
    """A line that was wrong in at least one run."""

    model_config = ConfigDict(frozen=True)

    text: str
    wrong: int  # runs in which it was wrong
    expected: str  # the closest acceptable answer, as the runner prints it
    got: tuple[str, ...]  # each distinct wrong result, sorted


class CaseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    case: str
    reader: str
    group: str
    why: str = ""
    lines: int = 0
    runs: int
    passed: int
    pass_rate: float
    fields: float  # the share of scored fields that were right, over all runs
    seconds_per_run: float
    output_tokens_per_run: int
    errors: tuple[str, ...] = ()
    misses: tuple[Miss, ...] = ()


class Results(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str
    repeat: int
    started: datetime | None = None
    seconds: float | None = None  # wall time for the whole run
    cases: dict[str, CaseResult]


def load_results(path: Path) -> Results:
    return Results.model_validate_json(path.read_text())
