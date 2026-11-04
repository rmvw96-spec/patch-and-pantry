import pytest
from pydantic_ai import models
from pydantic_ai.models.test import TestModel

from app.agents.study_agent import SimCall, SimClient, reviewer_agent

# Safety switch: any request to a real model provider now raises. TestModel and FunctionModel are unaffected,
# so this suite needs no API keys and can never spend model budget by accident.
models.ALLOW_MODEL_REQUESTS = False


class FakeSimClient(SimClient):
    """Deterministic stand-in for the simulator that records every call it receives."""

    def __init__(self, max_inlet_bar: float = 80.0) -> None:
        super().__init__(max_inlet_bar)
        self.calls: list[SimCall] = []

    async def run(self, case_id: str, inlet_bar: float) -> dict:
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


@pytest.fixture
def plausible_reviewer():
    """Override the sub-agent so any test that reaches request_review gets a fixed answer, offline."""
    with reviewer_agent.override(
        model=TestModel(custom_output_text="Looks physically plausible.")
    ):
        yield
