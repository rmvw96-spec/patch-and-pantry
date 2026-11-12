# ruff: noqa: T201  -- a command-line script: printing is its output
"""Live smoke check of a Foundry model. Manual only: it calls a live model and costs money.

From backend/, with FOUNDRY_ENDPOINT and FOUNDRY_API_KEY in ../.env.local:

    docker compose up -d phoenix
    PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006/v1/traces \\
        uv run python scripts/smoke_foundry.py [foundry:deepseek-v3.2]

Exits non-zero if the model does not make real tool calls or the study fails.
"""

import asyncio
import sys

from app.agents.models import build_model
from app.agents.smoke import ToolCallCheckFailed, check_tool_calls, run_smoke_study
from app.core.config import settings
from app.core.tracing import setup_tracing, shutdown_tracing

DEFAULT_SPEC = "foundry:deepseek-v3.2"


async def main(spec: str) -> int:
    if not spec.startswith("foundry:"):
        print(f"Expected a foundry: model spec, got {spec!r}", file=sys.stderr)
        return 2
    # The reviewer agent builds its model from settings.SIM_MODEL, so point it at the same model.
    settings.SIM_MODEL = spec
    model = build_model(spec)
    if not settings.PHOENIX_COLLECTOR_ENDPOINT:
        print("Tracing is off (PHOENIX_COLLECTOR_ENDPOINT unset): no trace in Phoenix.")
    setup_tracing(
        endpoint=settings.PHOENIX_COLLECTOR_ENDPOINT,
        include_content=settings.TRACE_INCLUDE_CONTENT,
    )
    try:
        print(f"1/2 Tool-call check with {spec} ...")
        try:
            await check_tool_calls(model)
        except ToolCallCheckFailed as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1
        print("    ok: the model made a structured tool call")

        print("2/2 Smoke study ...")
        report = await run_smoke_study(model)
        print(f"    result:            {report.result.model_dump()}")
        print(f"    trace ID:          {report.trace_id}")
        print(f"    model requests:    {report.requests}")
        print(f"    input tokens:      {report.input_tokens}")
        print(f"    output tokens:     {report.output_tokens}")
        print(f"    cache read tokens: {report.cache_read_tokens}")
        return 0
    finally:
        shutdown_tracing()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SPEC)))
