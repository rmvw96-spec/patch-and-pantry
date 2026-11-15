# ruff: noqa: T201  -- a command-line tool: printing is its output
"""Write the baseline (stable, flaky, and broken cases and lines) from a reviewed run's results.

    uv run python -m evals --repeat 5 --json results.json
    uv run python -m evals.make_baseline results.json          # writes evals/baseline.json

The baseline changes only by a reviewed PR (spec P1-9, D15).
"""

import argparse
import sys
from pathlib import Path

from evals.baseline import make_baseline
from evals.cases import CASES_DIR, load_cases
from evals.results import load_results

BASELINE = Path(__file__).parent / "baseline.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "results", type=Path, help="a results file from python -m evals --json"
    )
    parser.add_argument("-o", "--output", type=Path, default=BASELINE)
    parser.add_argument("--dir", type=Path, default=CASES_DIR, help="case files")
    args = parser.parse_args(argv)

    baseline = make_baseline(load_results(args.results), load_cases(args.dir))
    args.output.write_text(baseline.model_dump_json(indent=2) + "\n")
    tiers = ", ".join(f"{baseline.count(t)} {t}" for t in ("stable", "flaky", "broken"))
    print(f"Wrote {args.output}: {tiers} (cases and readers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
