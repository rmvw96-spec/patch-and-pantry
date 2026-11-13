# ruff: noqa: T201  -- a command-line tool: printing is its output
"""Run the golden evals for typed price lists (spec P1-9).

From backend/, with the catalog loaded (``python -m app.seed all``) and, for a live model,
FOUNDRY_ENDPOINT and FOUNDRY_API_KEY in ../.env.local:

    uv run python -m evals --cases extraction            # SIM_MODEL, both readers, 3 runs
    uv run python -m evals --model demo --repeat 1       # free: the demo reader
    uv run python -m evals --case aldi-counts --reader model --verbose

Each case runs with the model reading every line (``model``, which watches the model itself) and
with rules first (``rules_first``, as the app reads lists), unless ``--reader`` picks one. Nothing
is saved: each run is rolled back. Exit status 1 when a case fails.
"""

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic_ai.models import Model
from pydantic_evals import Case as EvalCase
from pydantic_evals import Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from app.agents.models import build_model
from app.core.config import CaptureReader, settings
from evals.capture import (
    CaseRun,
    LineScore,
    RetryOnRateLimit,
    expected_fields,
    run_case,
    score_case,
    show_expect,
)
from evals.cases import CASES_DIR, EXTRACTION_THRESHOLD, GROUPS, Case, load_cases

READERS: tuple[CaptureReader, ...] = ("model", "rules_first")
Inputs = tuple[Case, CaptureReader]


def passes(case: Case, lines: list[LineScore]) -> bool:
    right = sum(s.right for s in lines)
    scored = sum(s.scored for s in lines)
    if case.group == "extraction":
        return right >= EXTRACTION_THRESHOLD * scored
    return right == scored


@dataclass
class FieldScore(Evaluator[Inputs, CaseRun, None]):
    """The share of the case's fields that are right, and whether the run passes."""

    def evaluate(self, ctx: EvaluatorContext[Inputs, CaseRun, None]) -> dict[str, Any]:
        case, _reader = ctx.inputs
        lines = score_case(case, ctx.output)
        scored = sum(s.scored for s in lines)
        return {
            "fields": sum(s.right for s in lines) / scored,
            "passed": passes(case, lines),
        }


@dataclass
class Tally:
    """One case with one reader, over its runs."""

    case: Case
    reader: str
    runs: int = 0
    passed: int = 0
    right: int = 0
    scored: int = 0
    seconds: float = 0.0
    output_tokens: int = 0
    model_lines: int = 0
    errors: list[str] = field(default_factory=list)
    misses: dict[str, list[LineScore]] = field(
        default_factory=lambda: defaultdict(list)
    )


def tally(cases: list[Case], report: Any) -> list[Tally]:
    found: dict[tuple[str, str], Tally] = {}
    for case in cases:
        for reader in READERS:
            found[case.name, reader] = Tally(case, reader)
    for done in report.cases:
        case, reader = done.inputs
        t = found[case.name, reader]
        lines = score_case(case, done.output)
        t.runs += 1
        t.passed += passes(case, lines)
        t.right += sum(s.right for s in lines)
        t.scored += sum(s.scored for s in lines)
        t.seconds += done.task_duration
        t.output_tokens += done.output.usage.output_tokens
        t.model_lines += done.output.model_lines
        for s in lines:
            if s.wrong:
                t.misses[s.text].append(s)
    for failed in report.failures:
        case, reader = failed.inputs
        t = found[case.name, reader]
        t.runs += 1
        t.scored += sum(len(expected_fields(line.expect[0])) for line in case.lines)
        t.errors.append(failed.error_message)
    return [t for t in found.values() if t.runs]


def print_report(tallies: list[Tally], repeat: int, verbose: bool) -> None:
    print(
        f"\n{'case':<34} {'group':<13} {'reader':<11} {'passed':>7} {'fields':>7}"
        f" {'s/run':>6} {'out tok':>8}"
    )
    for t in tallies:
        share = t.right / t.scored if t.scored else 0.0
        print(
            f"{t.case.name:<34} {t.case.group:<13} {t.reader:<11}"
            f" {t.passed:>3}/{t.runs:<3} {share:>6.0%} {t.seconds / t.runs:>6.1f}"
            f" {t.output_tokens // t.runs:>8}"
        )
    missed = [t for t in tallies if t.misses or t.errors]
    if not missed:
        return
    print("\nMisses, including in passing cases (expected, then what the runs got):")
    for t in missed:
        status = "passed" if t.passed == t.runs else "FAILED"
        print(f"\n  {t.case.name} [{t.reader}] {status}: {t.case.why}")
        for error in t.errors:
            print(f"    run failed: {error}")
        for text, misses in t.misses.items():
            print(f"    {text!r}  ({len(misses)} of {repeat} runs wrong)")
            print(f"      expected {show_expect(misses[0].closest)}")
            seen = sorted({m.got.show() if m.got else "(no outcome)" for m in misses})
            for got in seen if verbose else seen[:1]:
                print(f"      got      {got}")


def summary(tallies: list[Tally], model: str, repeat: int) -> dict[str, Any]:
    return {
        "model": model,
        "repeat": repeat,
        "cases": {
            f"{t.case.name} [{t.reader}]": {
                "group": t.case.group,
                "pass_rate": t.passed / t.runs,
                "fields": t.right / t.scored if t.scored else 0.0,
                "seconds_per_run": round(t.seconds / t.runs, 2),
                "output_tokens_per_run": t.output_tokens // t.runs,
            }
            for t in tallies
        },
    }


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", choices=[*GROUPS, "all"], default="all")
    parser.add_argument("--case", action="append", help="only this case (repeatable)")
    parser.add_argument("--reader", choices=[*READERS, "both"], default="both")
    parser.add_argument(
        "--model", default=settings.SIM_MODEL, help="default: SIM_MODEL"
    )
    parser.add_argument("--repeat", type=int, default=3, help="runs per case (D2: 3)")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="cases at once (Foundry limits tokens a minute)",
    )
    parser.add_argument("--json", type=Path, help="write per-case results here")
    parser.add_argument("--dir", type=Path, default=CASES_DIR, help="case files")
    parser.add_argument("--verbose", action="store_true", help="every distinct miss")
    args = parser.parse_args(argv)

    cases = [
        c
        for c in load_cases(args.dir)
        if (args.cases == "all" or c.group == args.cases)
        and (not args.case or c.name in args.case)
    ]
    if not cases:
        print("No cases match.", file=sys.stderr)
        return 2
    readers = READERS if args.reader == "both" else (args.reader,)
    built = build_model(args.model, role="capture")
    model = RetryOnRateLimit(built) if isinstance(built, Model) else built

    async def task(inputs: Inputs) -> CaseRun:
        case, reader = inputs
        return await run_case(case, model, reader)

    dataset = Dataset[Inputs, CaseRun, None](
        name="typed-lists",
        cases=[
            EvalCase(name=f"{c.name} [{r}]", inputs=(c, r))
            for c in cases
            for r in readers
        ],
        evaluators=[FieldScore()],
    )
    report = await dataset.evaluate(
        task,
        name=f"typed-lists {args.model}",
        repeat=args.repeat,
        max_concurrency=args.concurrency,
        progress=False,
    )
    tallies = tally(cases, report)
    print(f"{len(cases)} cases, readers: {', '.join(readers)}, model: {args.model}")
    print_report(tallies, args.repeat, args.verbose)
    if args.json:
        args.json.write_text(
            json.dumps(summary(tallies, args.model, args.repeat), indent=2) + "\n"
        )
    return 0 if all(t.passed == t.runs for t in tallies) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
