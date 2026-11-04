"""Fixtures for agent tests. pytest applies this file only to tests in tests/agents/.

The live-model safety switch lives in tests/conftest.py, so it protects every test in the backend.
Model overrides are entered inside each test body rather than in fixtures here.
"""

import pytest

from app.agents.study_agent import SimCall, SimClient, SimOutput


class FakeSimClient(SimClient):
    """Deterministic stand-in for the simulator that records every call it receives."""

    def __init__(self, max_inlet_bar: float = 80.0) -> None:
        super().__init__(max_inlet_bar)
        self.calls: list[SimCall] = []

    async def run(self, case_id: str, inlet_bar: float) -> SimOutput:
        self.calls.append(SimCall(case_id, inlet_bar))
        return {
            "case_id": case_id,
            "inlet_bar": inlet_bar,
            "outlet_bar": round(inlet_bar * 0.9, 3),
        }


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake_sim() -> FakeSimClient:
    return FakeSimClient(max_inlet_bar=80.0)
