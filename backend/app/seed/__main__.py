# ruff: noqa: T201  -- a command-line entry point: printing is its output
"""Run seed loaders by hand. Never runs in CI.

From backend/:

    uv run python -m app.seed ers_prices ../data/sources/usda-ers/all-vegetables-average-prices.csv
    uv run python -m app.seed all ~/Downloads/FoodData_Central_*.zip ~/Downloads/sr28asc.zip
    uv run python -m app.seed report

Each file is loaded inside a Prefect flow and committed on its own; loading the same file again
changes nothing. See app/seed_data/README.md for where each file comes from.
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from prefect import flow
from sqlmodel import Session

from app.core.db import engine
from app.seed.crosswalk import SEED_DATA
from app.seed.loaders import (
    load_ers_prices,
    load_fdc_subset,
    load_foodkeeper,
    load_garden_yields,
    load_planting_calendar,
    load_sr28_portions,
    load_usda_zip,
)
from app.seed.report import coverage_gaps, format_report
from app.seed.snapshots import LoadResult

Loader = Callable[[Session, Path], LoadResult]

LOADERS: dict[str, Loader] = {
    "ers_prices": load_ers_prices,
    "planting_calendar": load_planting_calendar,
    "garden_yields": load_garden_yields,
    "foodkeeper": load_foodkeeper,
    "fdc": load_fdc_subset,
    "sr28_portions": load_sr28_portions,
    # Either of the two above, chosen by what the zip contains.
    "usda_zip": load_usda_zip,
}

REPO = Path(__file__).resolve().parents[3]
SOURCES = REPO / "data" / "sources"
# Files committed to the repository, in load order. USDA zips (FDC, SR28) are not committed.
COMMITTED: list[tuple[str, Path]] = [
    ("ers_prices", SOURCES / "usda-ers" / "all-vegetables-average-prices.csv"),
    ("ers_prices", SOURCES / "usda-ers" / "all-fruits-average-prices.csv"),
    ("planting_calendar", SEED_DATA / "north_alabama_planting_calendar.csv"),
    ("garden_yields", SEED_DATA / "minnesota_home_garden_yields.csv"),
    ("foodkeeper", SOURCES / "usda-fsis" / "foodkeeper.json"),
]

SessionFactory = Callable[[], Session]


def _default_session() -> Session:
    return Session(engine)


def load_files(
    jobs: Sequence[tuple[str, Path]], session_factory: SessionFactory | None = None
) -> list[str]:
    """Load each file and commit it on its own, so one bad file leaves the others loaded."""
    # Looked up at call time (not as a default argument), so tests can replace it.
    make_session = session_factory or _default_session
    summaries = []
    for loader, path in jobs:
        with make_session() as session:
            result = LOADERS[loader](session, path)
            session.commit()
            summaries.append(f"{path.name}: {result.summary()}")
    return summaries


@flow(name="seed-load")
def seed_flow(jobs: list[tuple[str, Path]]) -> list[str]:
    return load_files(jobs)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.seed", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in LOADERS:
        command = commands.add_parser(name, help=f"load {name} from local files")
        command.add_argument("paths", nargs="+", type=Path)
    everything = commands.add_parser(
        "all", help="load every committed source, then USDA zips (FDC and SR28)"
    )
    everything.add_argument("zips", nargs="*", type=Path)
    commands.add_parser("report", help="list first-crop-set items with gaps")
    args = parser.parse_args(argv)

    if args.command == "report":
        with _default_session() as session:
            print(format_report(coverage_gaps(session)))
        return 0
    if args.command == "all":
        jobs = COMMITTED + [("usda_zip", path) for path in args.zips]
    else:
        jobs = [(args.command, path) for path in args.paths]
    for summary in seed_flow(jobs):
        print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
