#!/usr/bin/env bash

set -e
set -x

FASTAPI_ENV=development uv run -m pytest tests/agents
