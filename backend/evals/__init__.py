"""Golden evals (spec P1-9): typed price lists read by the live model, scored line by line.

Run from backend/: ``uv run python -m evals --cases extraction`` (see ``evals/README.md``). Cases
call ``SIM_MODEL`` (a live model costs money); ``--model demo`` runs them free with the demo
reader, to check the cases and the runner.
"""
