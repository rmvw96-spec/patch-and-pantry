"""Prototype: cost of one 4 x 8 ft raised bed from captured Shoals-area prices (Nov 2026).

This is a worked example, not app code. Phase 3's `plan_grow_vs_buy` tool should reimplement this
logic with prices from the price book instead of the constants below. The numbers it prints are
recorded as known answers in docs/specs/known-answers.md.

Usage, from the repository root:
    uv run python scripts/data/bed_costs.py
"""

import math

BED_LENGTH_FT, BED_WIDTH_FT = 8, 4
BOARDS_PER_ROW = 3  # two 8 ft sides, plus one 8 ft board cut into two 4 ft ends
CHEAPEST_BAG_USD_PER_CU_FT = (
    2.90  # Evergreen 1 cu ft bag, Lowe's Florence (listing, unverified)
)

# USD per cubic foot, from captured prices (2026-11-11)
SOIL_USD_PER_CU_FT = {
    "Evergreen 1 cu ft bag, Lowe's (2.90)": 2.90 / 1.0,
    "All-purpose 0.75 cu ft bag, Rural King (2.99)": 2.99 / 0.75,
    "Scotts 0.75 cu ft bag, Lowe's (3.28)": 3.28 / 0.75,
    "Scotts 0.75 cu ft bag, Tractor Supply (3.99)": 3.99 / 0.75,
}

# name: (low USD per board, high USD per board, actual board width in inches)
FRAMES = {
    "1x8 cedar, Home Depot (25.65; actual 0.625 x 7.5 in)": (25.65, 25.65, 7.5),
    "2x8 cedar, regional (40.94-52.80; actual 7.25 in)": (40.94, 52.80, 7.25),
}

BULK_USD_PER_YD = 62.00  # Mulch Mound Florence, screened topsoil, verified 2026-11-11
BULK_MIN_YD = 3
CU_FT_PER_YD = 27


def bed_volume_cu_ft(depth_in: float) -> float:
    return BED_LENGTH_FT * BED_WIDTH_FT * depth_in / 12


def main() -> None:
    print(
        "One 4x8 ft bed: frame + cheapest bagged soil (screws and compost not included)"
    )
    for name, (low, high, width_in) in FRAMES.items():
        for rows in (1, 2):
            depth = width_in * rows
            cu_ft = bed_volume_cu_ft(depth)
            bags = math.ceil(cu_ft)
            soil = bags * CHEAPEST_BAG_USD_PER_CU_FT
            frame_low, frame_high = (
                low * BOARDS_PER_ROW * rows,
                high * BOARDS_PER_ROW * rows,
            )
            print(
                f"  {name}, {rows} row(s): depth {depth:.2f} in, soil {cu_ft:.1f} cu ft "
                f"({bags} bags), frame {frame_low:.2f}-{frame_high:.2f}, soil {soil:.2f}, "
                f"total {frame_low + soil:.2f}-{frame_high + soil:.2f}"
            )

    print("\nSoil price per cubic foot:")
    for name, per in sorted(SOIL_USD_PER_CU_FT.items(), key=lambda kv: kv[1]):
        print(f"  {name}: {per:.2f}")

    bulk_cu_ft = BULK_MIN_YD * CU_FT_PER_YD
    bulk_cost = BULK_USD_PER_YD * BULK_MIN_YD
    bag_cost = bulk_cu_ft * CHEAPEST_BAG_USD_PER_CU_FT
    print(
        f"\nBulk minimum: {BULK_MIN_YD} yd = {bulk_cu_ft} cu ft for {bulk_cost:.2f} plus delivery; "
        f"the same volume in bags costs {bag_cost:.2f}, so bulk wins only if delivery "
        f"< {bag_cost - bulk_cost:.2f}"
    )
    print(
        f"Check: a 4x8 bed 12 in deep needs {bed_volume_cu_ft(12):.0f} cu ft = "
        f"{bed_volume_cu_ft(12) / CU_FT_PER_YD:.2f} yd (the vendor FAQ says about 0.4 yd)"
    )


if __name__ == "__main__":
    main()
