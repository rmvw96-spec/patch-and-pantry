"""Seed loaders: versioned reference data from local source files (Phase 0, P0-4).

Each loader takes a session and a local file, registers the file as a data snapshot (SHA-256),
and writes rows that point at that snapshot. Loading a file whose (source, hash) is already
registered changes nothing. Loaders flush but never commit; the command line (``python -m
app.seed``) commits once per file. Downloads are a separate, manual step: see
``app/seed_data/README.md``.
"""
