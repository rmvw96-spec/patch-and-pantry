"""Fixtures for agent tests. pytest applies this file only to tests in tests/agents/.

The live-model safety switch lives in tests/conftest.py, so it protects every test in the backend.
Model overrides are entered inside each test body rather than in fixtures here.
"""

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
