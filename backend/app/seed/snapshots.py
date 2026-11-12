"""Register source files as immutable data snapshots."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select

from app.models import DataSnapshot, SnapshotSource


@dataclass(frozen=True)
class SourceInfo:
    """Where a dataset comes from, recorded on every snapshot of it."""

    url: str
    version: str
    retrieved_at: datetime
    notes: str | None = None


@dataclass
class LoadResult:
    snapshot: DataSnapshot
    created: bool  # False when this exact file was already loaded: nothing was written
    rows_written: int

    def summary(self) -> str:
        if not self.created:
            return f"{self.snapshot.source} {self.snapshot.version}: already loaded, no changes"
        return (
            f"{self.snapshot.source} {self.snapshot.version}: snapshot "
            f"{self.snapshot.sha256[:12]}, {self.rows_written} rows written"
        )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def register_snapshot(
    session: Session,
    *,
    source: SnapshotSource,
    path: Path,
    info: SourceInfo,
    row_count: int,
) -> tuple[DataSnapshot, bool]:
    """Return the snapshot for this file, creating it only if (source, hash) is new."""
    sha256 = file_sha256(path)
    existing = session.exec(
        select(DataSnapshot).where(
            DataSnapshot.source == source, DataSnapshot.sha256 == sha256
        )
    ).first()
    if existing is not None:
        return existing, False
    snapshot = DataSnapshot(
        source=source,
        version=info.version,
        retrieved_at=info.retrieved_at,
        url=info.url,
        sha256=sha256,
        row_count=row_count,
        notes=info.notes,
    )
    session.add(snapshot)
    session.flush()
    return snapshot, True
