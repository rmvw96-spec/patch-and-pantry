"""Toy pipeline simulator, run as a Prefect flow.

Stands in for a physics simulator: given a pipeline case and an inlet pressure, it returns the
outlet pressure. The work runs as a Prefect task that retries failures, the way a real simulator
call would be retried, and the agent reaches it through PipeSimClient.
"""

import asyncio
import math
from dataclasses import dataclass

from prefect import flow, task

from app.agents.study_agent import SimClient, SimOutput
from app.core.config import settings


@dataclass(frozen=True)
class PipeCase:
    description: str
    # Simplified gas-pipeline relation: outlet^2 = inlet^2 - drop_coefficient (pressures in bar).
    # Real flow equations (for example Weymouth) derive this coefficient from flow rate, length,
    # diameter, and gas properties.
    drop_coefficient: float


CASES: dict[str, PipeCase] = {
    "A": PipeCase("50 km trunk line", drop_coefficient=1100.0),
    "B": PipeCase("120 km export line", drop_coefficient=2000.0),
    "C": PipeCase("15 km gathering line", drop_coefficient=400.0),
}


class UnknownCaseError(ValueError):
    """Raised for a case_id the simulator does not know. A ValueError, so the agent can retry."""


def solve_outlet_pressure(case: PipeCase, inlet_bar: float) -> float:
    """Outlet pressure in bar. 0.0 means the line cannot deliver gas at this inlet pressure."""
    squared = inlet_bar**2 - case.drop_coefficient
    return round(math.sqrt(squared), 3) if squared > 0 else 0.0


@task(retries=2, retry_delay_seconds=0)
async def simulate_pipe(case_id: str, inlet_bar: float) -> SimOutput:
    if settings.SIM_DELAY_SECONDS:
        await asyncio.sleep(settings.SIM_DELAY_SECONDS)
    outlet_bar = solve_outlet_pressure(CASES[case_id], inlet_bar)
    return {"case_id": case_id, "inlet_bar": inlet_bar, "outlet_bar": outlet_bar}


@flow(name="simulate-case")
async def simulate_case(case_id: str, inlet_bar: float) -> SimOutput:
    return await simulate_pipe(case_id, inlet_bar)


class PipeSimClient(SimClient):
    """Production simulator client: each call runs the simulate-case Prefect flow."""

    async def run(self, case_id: str, inlet_bar: float) -> SimOutput:
        if case_id not in CASES:
            known = ", ".join(sorted(CASES))
            raise UnknownCaseError(f"unknown case_id {case_id!r}; known cases: {known}")
        return await simulate_case(case_id, inlet_bar)
