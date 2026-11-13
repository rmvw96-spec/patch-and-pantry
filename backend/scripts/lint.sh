#!/usr/bin/env bash

set -e
set -x

mypy app evals
ty check app evals
ruff check app evals
ruff format app evals --check
