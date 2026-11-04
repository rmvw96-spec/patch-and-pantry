"""Tests for the deterministic demo model (SIM_MODEL=demo).

Runs the demo model through the real study agent and the real simulator equations. The simulator
is called directly rather than through Prefect, which keeps these tests fast.
"""

import pytest

from app.agents.demo_model import demo_study_model, parse_prompt
from app.agents.simulator import CASES, UnknownCaseError, solve_outlet_pressure
from app.agents.study_agent import SimClient, SimOutput, run_study


class DirectSimClient(SimClient):
    """The real simulator equations, called directly instead of through a Prefect flow."""

    async def run(self, case_id: str, inlet_bar: float) -> SimOutput:
        if case_id not in CASES:
            raise UnknownCaseError(f"unknown case_id {case_id!r}")
        outlet = solve_outlet_pressure(CASES[case_id], inlet_bar)
        return {"case_id": case_id, "inlet_bar": inlet_bar, "outlet_bar": outlet}


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("Optimize case B at 70 bar", ("B", 70.0)),
        ("check case c, inlet 45.5 bar please", ("C", 45.5)),
        ("What's the outlet pressure?", ("A", 60.0)),  # defaults
    ],
)
def test_parse_prompt(prompt: str, expected: tuple[str, float]) -> None:
    assert parse_prompt(prompt) == expected


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("prompt", "objective", "feasible"),
    [
        ("Optimize case A", 50.0, True),  # sqrt(60^2 - 1100)
        ("Optimize case B at 70 bar", 53.852, True),  # sqrt(70^2 - 2000)
        ("Optimize case B at 40 bar", 0.0, False),  # line cannot deliver
        ("Optimize case A at 95 bar", 0.0, False),  # over the 80 bar limit: rejected
        ("Optimize case Z", 0.0, False),  # unknown case: rejected
    ],
)
async def test_demo_outcomes(prompt: str, objective: float, feasible: bool) -> None:
    result = await run_study(
        prompt, DirectSimClient(max_inlet_bar=80.0), model=demo_study_model()
    )

    assert result.objective == objective
    assert result.feasible is feasible
