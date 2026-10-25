"""SQLite WAL state ledger.

Every job and artifact is journaled *before* the work happens, so a crash,
Ctrl-C or power loss leaves an exact, replayable record:

* ``jobs``      - one row per URL/mode with a PENDING -> DOWNLOADING ->
                   PROCESSING -> COMPLETED | FAILED lifecycle
* ``artifacts`` - staged chunks/finals with byte counts and SHA-256 sums
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..errors import LedgerError

STATUSES = ("PENDING", "DOWNLOADING", "PROCESSING", "COMPLETED", "FAILED", "SKIPPED")
TERMINAL = {"COMPLETED", "FAILED", "SKIPPED"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    mode TEXT NOT NULL,
    title TEXT,
    playlist_id TEXT,
    playlist_index INTEGER,
    status TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    selector TEXT,
    rung INTEGER,
    output_path TEXT,
    checksum TEXT,
    bytes INTEGER DEFAULT 0,
    error TEXT,
    meta TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(url, mode)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_playlist ON jobs(playlist_id, playlist_index);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    bytes INTEGER DEFAULT 0,
    sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT,
    created_at REAL NOT NULL
);
"""


@dataclass
class Job:
    id: int
    url: str
    mode: str
    status: str
    title: str | None = None
    playlist_id: str | None = None
    playlist_index: int | None = None
    attempts: int = 0
    selector: str | None = None
    rung: int | None = None
    output_path: str | None = None
    checksum: str | None = None
    bytes: int = 0
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        meta = row["meta"]
        try:
            meta_obj = json.loads(meta) if meta else {}
        except json.JSONDecodeError:  # pragma: no cover
            meta_obj = {}
        return cls(
            id=row["id"],
            url=row["url"],
            mode=row["mode"],
            status=row["status"],
            title=row["title"],
            playlist_id=row["playlist_id"],
            playlist_index=row["playlist_index"],
            attempts=row["attempts"],
            selector=row["selector"],
            rung=row["rung"],
            output_path=row["output_path"],
            checksum=row["checksum"],
            bytes=row["bytes"] or 0,
            error=row["error"],
            meta=meta_obj,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class Ledger:
    """Thread-safe WAL ledger (single connection + RLock)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            with self._lock:
                self._conn.executescript(_SCHEMA)
                self._conn.commit()
        except sqlite3.Error as exc:  # pragma: no cover
            raise LedgerError(f"Cannot open ledger at {self.path}: {exc}") from exc

    # ------------------------------------------------------------ plumbing
    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --------------------------------------------------------------- jobs
    def create_job(
        self,
        url: str,
        mode: str,
        title: str | None = None,
        playlist_id: str | None = None,
        playlist_index: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Job:
        now = time.time()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO jobs (url, mode, title, playlist_id, playlist_index, status,
                                  meta, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
                ON CONFLICT(url, mode) DO UPDATE SET
                    title = COALESCE(jobs.title, excluded.title),
                    playlist_id = COALESCE(jobs.playlist_id, excluded.playlist_id),
                    playlist_index = COALESCE(jobs.playlist_index, excluded.playlist_index),
                    meta = COALESCE(excluded.meta, jobs.meta),
                    updated_at = excluded.updated_at
                """,
                (url, mode, title, playlist_id, playlist_index, json.dumps(meta or {}), now, now),
            )
        job = self.get_job(url, mode)
        if job is None:  # pragma: no cover - defensive
            raise LedgerError("Job insert did not persist")
        return job

    def get_job(self, url: str, mode: str) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE url = ? AND mode = ?", (url, mode)
            ).fetchone()
        return Job.from_row(row) if row else None

    def get_job_by_id(self, job_id: int) -> Job | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def mark(
        self,
        url: str,
        mode: str,
        status: str,
        *,
        title: str | None = None,
        error: str | None = None,
        output_path: str | None = None,
        checksum: str | None = None,
        bytes_written: int | None = None,
        selector: str | None = None,
        rung: int | None = None,
        bump_attempts: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> Job:
        if status not in STATUSES:
            raise LedgerError(f"Unknown status '{status}' (expected one of {STATUSES})")
        fields: dict[str, Any] = {"status": status, "updated_at": time.time()}
        if title is not None:
            fields["title"] = title
        if error is not None:
            fields["error"] = error
        if output_path is not None:
            fields["output_path"] = output_path
        if checksum is not None:
            fields["checksum"] = checksum
        if bytes_written is not None:
            fields["bytes"] = bytes_written
        if selector is not None:
            fields["selector"] = selector
        if rung is not None:
            fields["rung"] = rung
        if meta is not None:
            fields["meta"] = json.dumps(meta)
        assignments = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values())
        with self._tx() as conn:
            cur = conn.execute(
                f"UPDATE jobs SET {assignments} WHERE url = ? AND mode = ?",
                (*values, url, mode),
            )
            if cur.rowcount == 0:
                raise LedgerError(f"No job for {url} [{mode}]")
            if bump_attempts:
                conn.execute(
                    "UPDATE jobs SET attempts = attempts + 1 WHERE url = ? AND mode = ?",
                    (url, mode),
                )
        job = self.get_job(url, mode)
        assert job is not None
        return job

    def retryable(self, modes: Iterable[str] | None = None) -> list[Job]:
        """Jobs that are not terminal, or failed jobs ready for another attempt."""
        query = "SELECT * FROM jobs WHERE status NOT IN ('COMPLETED','SKIPPED')"
        params: list[Any] = []
        if modes:
            modes = list(modes)
            query += f" AND mode IN ({','.join('?' * len(modes))})"
            params.extend(modes)
        query += " ORDER BY playlist_id, playlist_index, id"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [Job.from_row(r) for r in rows]

    def jobs(self, status: str | None = None) -> list[Job]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY id", (status,)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        return [Job.from_row(r) for r in rows]

    def pending(self) -> list[Job]:
        return self.jobs("PENDING")

    def stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            ).fetchall()
        counts = {status: 0 for status in STATUSES}
        for row in rows:
            counts[row["status"]] = row["n"]
        counts["TOTAL"] = sum(counts[s] for s in STATUSES)
        return counts

    # ---------------------------------------------------------- artifacts
    def record_artifact(
        self,
        job_id: int,
        kind: str,
        path: str | Path,
        *,
        bytes_written: int | None = None,
        sha256: str | None = None,
        status: str = "PENDING",
    ) -> int:
        now = time.time()
        with self._tx() as conn:
            cur = conn.execute(
                """
                INSERT INTO artifacts (job_id, kind, path, bytes, sha256, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, kind, str(path), bytes_written or 0, sha256, status, now, now),
            )
            return int(cur.lastrowid or 0)

    def update_artifact(
        self,
        artifact_id: int,
        *,
        status: str | None = None,
        bytes_written: int | None = None,
        sha256: str | None = None,
        path: str | Path | None = None,
    ) -> None:
        fields: dict[str, Any] = {"updated_at": time.time()}
        if status is not None:
            fields["status"] = status
        if bytes_written is not None:
            fields["bytes"] = bytes_written
        if sha256 is not None:
            fields["sha256"] = sha256
        if path is not None:
            fields["path"] = str(path)
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._tx() as conn:
            conn.execute(
                f"UPDATE artifacts SET {assignments} WHERE id = ?", (*fields.values(), artifact_id)
            )

    def artifacts(self, job_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM artifacts WHERE job_id = ? ORDER BY id", (job_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- events
    def log(self, message: str, level: str = "info", job_id: int | None = None,
            data: dict[str, Any] | None = None) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO events (job_id, level, message, data, created_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, level, message, json.dumps(data or {}), time.time()),
            )

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]
