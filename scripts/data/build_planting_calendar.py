# /// script
# requires-python = ">=3.12"
# dependencies = ["pdfplumber>=0.11"]
# ///
"""Build the North Alabama planting-calendar seed file from Alabama Extension's chart.

The chart's dates are for central Alabama. Alabama Extension's planting guide shifts North
Alabama about 10 days later in spring and 10 days earlier in fall; this script applies that rule.
The derived dates are UNVERIFIED: Extension's dedicated North Alabama table (Vegetable Seasons in
Alabama, ANR-1165) reports later, wider fall windows for some crops. See docs/design.md.

Usage, from the repository root:
    uv run scripts/data/build_planting_calendar.py
"""

import argparse
import calendar
import csv
import re
from datetime import date, timedelta
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PDF = (
    ROOT / "data/sources/alabama-extension/alabama-vegetable-garden-planting-chart.pdf"
)
DEFAULT_OUT = ROOT / "backend/app/seed_data/north_alabama_planting_calendar.csv"
RETRIEVED = "2026-11-11"
SOURCE = "Alabama Cooperative Extension, Alabama Vegetable Garden Planting Chart (central Alabama dates)"

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS["sept"] = 9
BASE_YEAR = 2027  # any non-leap year; only month and day are used
SHIFT_DAYS = {"spring": +10, "fall": -10}
PERENNIALS = {"Asparagus"}
SPACING_FIXES = {"60x2436": "60x24-36"}  # typo in the source PDF


def extract_rows(pdf_path: Path) -> list[list[str]]:
    rows: list[list[str]] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    cells = [(c or "").replace("\n", " ").strip() for c in row]
                    if cells and cells[0] and not cells[0].startswith("Vegetable"):
                        rows.append(cells)
    return rows


def parse_part(text: str) -> tuple[int | None, int | None]:
    match = re.match(r"^\s*([A-Za-z]+)?\.?\s*(\d+)?\s*$", text)
    if not match or not (match.group(1) or match.group(2)):
        raise ValueError(f"cannot parse date part {text!r}")
    month = MONTHS[match.group(1).lower()] if match.group(1) else None
    day = int(match.group(2)) if match.group(2) else None
    return month, day


def parse_window(text: str) -> tuple[date, date] | None:
    """'Apr. 10 - May 10' -> (date, date). A month with no day spans the whole month."""
    clean = text.replace("***", "").strip()
    if not clean:
        return None
    parts = [p for p in re.split(r"\s*-\s*", clean) if p]
    start_month, start_day = parse_part(parts[0])
    if start_month is None:
        raise ValueError(f"window has no start month: {text!r}")
    if len(parts) == 1:
        end_month = start_month
        end_day = start_day or calendar.monthrange(BASE_YEAR, start_month)[1]
    else:
        month2, day2 = parse_part(parts[1])
        end_month = month2 or start_month
        end_day = day2 or calendar.monthrange(BASE_YEAR, end_month)[1]
    start = date(BASE_YEAR, start_month, start_day or 1)
    end = date(BASE_YEAR, end_month, end_day)
    if end < start:  # the window crosses the new year, e.g. October - February
        end = date(BASE_YEAR + 1, end_month, end_day)
    return start, end


def month_day(d: date) -> str:
    return f"{calendar.month_abbr[d.month]} {d.day}"


def build(rows: list[list[str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for veg, days, _cultivars, spring, fall, seeds, spacing in rows:
        maturity = re.findall(r"\d+", days)
        for season, text in (("spring", spring), ("fall", fall)):
            window = parse_window(text) if text else None
            if window is None:
                continue
            start, end = window
            shift = timedelta(days=SHIFT_DAYS[season])
            out.append(
                {
                    "crop": veg,
                    "season": season,
                    "central_al_window_as_published": text,
                    "central_al_start": month_day(start),
                    "central_al_end": month_day(end),
                    "north_al_start": month_day(start + shift),
                    "north_al_end": month_day(end + shift),
                    "days_to_maturity_min": maturity[0] if len(maturity) == 2 else "",
                    "days_to_maturity_max": maturity[1] if len(maturity) == 2 else "",
                    "seeds_or_plants_per_100ft": seeds,
                    "spacing_rows_x_plants_in": SPACING_FIXES.get(spacing, spacing),
                    "footnote_marker": "***" if "***" in text else "",
                    "in_scope": "no (perennial)" if veg in PERENNIALS else "yes",
                    "source": SOURCE,
                    "retrieved": RETRIEVED,
                    "north_al_rule": "spring +10 days, fall -10 days",
                }
            )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    rows = extract_rows(args.pdf)
    out = build(rows)
    with args.out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(out[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(out)
    print(f"{len(out)} planting windows from {len(rows)} crops -> {args.out}")


if __name__ == "__main__":
    main()
