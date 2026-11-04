"""Unit tests for the toy pipeline simulator.

The invariant tests check physics the answer must obey, rather than one memorized value:
outlet pressure never exceeds inlet pressure, and raising the inlet never lowers the outlet.
"""

import pytest

from app.agents.simulator import (
    CASES,
    PipeSimClient,
    UnknownCaseError,
    solve_outlet_pressure,
)


def test_known_answer_case_a() -> None:
    # sqrt(60^2 - 1100) = sqrt(2500) = 50
    assert solve_outlet_pressure(CASES["A"], 60.0) == 50.0


def test_line_that_cannot_deliver_returns_zero() -> None:
    # 40^2 = 1600 is below case B's drop coefficient of 2000
    assert solve_outlet_pressure(CASES["B"], 40.0) == 0.0


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_outlet_never_exceeds_inlet(case_id: str) -> None:
    for inlet in range(0, 121, 5):
        assert solve_outlet_pressure(CASES[case_id], float(inlet)) <= inlet


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_raising_inlet_never_lowers_outlet(case_id: str) -> None:
    outlets = [
        solve_outlet_pressure(CASES[case_id], float(p)) for p in range(0, 121, 5)
    ]
    assert outlets == sorted(outlets)


@pytest.mark.anyio
async def test_unknown_case_is_rejected_before_any_simulation() -> None:
    with pytest.raises(UnknownCaseError, match="known cases: A, B, C"):
        await PipeSimClient(max_inlet_bar=80.0).run("Z", 50.0)
