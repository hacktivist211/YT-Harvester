"""SQLite WAL ledger: status transitions, resume and artifacts."""

from __future__ import annotations

import threading

import pytest

from ytharvest.errors import LedgerError
from ytharvest.pipeline.ledger import STATUSES, Ledger


@pytest.fixture()
def ledger(tmp_path):
    with Ledger(tmp_path / "harvest.sqlite3") as db:
        yield db


def test_create_job_is_idempotent(ledger):
    job1 = ledger.create_job("https://youtu.be/a", "audio", title="A")
    job2 = ledger.create_job("https://youtu.be/a", "audio", title="A2")
    assert job1.id == job2.id
    assert job2.title == "A"          # first non-null title wins
    assert job2.status == "PENDING"


def test_status_lifecycle_to_completed(ledger):
    url = "https://youtu.be/b"
    ledger.create_job(url, "video", title="B")
    assert ledger.get_job(url, "video").status == "PENDING"

    ledger.mark(url, "video", "DOWNLOADING", selector="v+a", rung=1, bump_attempts=True)
    job = ledger.get_job(url, "video")
    assert (job.status, job.selector, job.rung, job.attempts) == ("DOWNLOADING", "v+a", 1, 1)

    ledger.mark(url, "video", "PROCESSING", checksum="deadbeef", bytes_written=1234)
    ledger.mark(url, "video", "COMPLETED", output_path="/tmp/out.mp4", checksum="cafe")
    job = ledger.get_job(url, "video")
    assert job.status == "COMPLETED"
    assert job.output_path == "/tmp/out.mp4"
    assert job.checksum == "cafe"
    assert job.bytes == 1234


def test_failed_job_records_error_and_attempts(ledger):
    url = "https://youtu.be/c"
    ledger.create_job(url, "video")
    ledger.mark(url, "video", "FAILED", error="HTTP Error 403: Forbidden", bump_attempts=True)
    job = ledger.get_job(url, "video")
    assert job.status == "FAILED"
    assert "403" in job.error
    assert job.attempts == 1
    # a failed job is retryable, a completed one is not
    assert [j.url for j in ledger.retryable()] == [url]
    ledger.mark(url, "video", "COMPLETED", output_path="/x.mp4")
    assert ledger.retryable() == []


def test_resume_flow_persists_intermediate_state(ledger):
    """A crash between phases must be visible after reopening the ledger."""
    url = "https://youtu.be/d"
    ledger.create_job(url, "audio", title="D")
    ledger.mark(url, "audio", "DOWNLOADING", meta={"staged": {"path": "/staging/D.webm", "bytes": 10}})
    ledger.mark(url, "audio", "PROCESSING", meta={
        "staged": {"path": "/staging/D.webm", "bytes": 10},
        "assembled": {"final_name": "D.flac", "artifact_id": 1},
    })

    # "restart": a brand new connection over the same file
    with Ledger(ledger.path) as reopened:
        job = reopened.get_job(url, "audio")
        assert job.status == "PROCESSING"
        assert job.meta["staged"]["path"] == "/staging/D.webm"
        assert job.meta["assembled"]["final_name"] == "D.flac"
        # phase 3 can pick up without re-querying youtube
        resumed = reopened.mark(url, "audio", "COMPLETED", output_path="/out/D.flac")
        assert resumed.status == "COMPLETED"


def test_unknown_status_rejected(ledger):
    ledger.create_job("https://youtu.be/e", "video")
    with pytest.raises(LedgerError):
        ledger.mark("https://youtu.be/e", "video", "DANCING")
    assert set(STATUSES) >= {"PENDING", "DOWNLOADING", "PROCESSING", "COMPLETED", "FAILED"}


def test_mark_missing_job_raises(ledger):
    with pytest.raises(LedgerError):
        ledger.mark("https://youtu.be/none", "video", "PENDING")


def test_stats_and_filters(ledger):
    for i, status in enumerate(["PENDING", "PENDING", "DOWNLOADING", "COMPLETED", "FAILED"]):
        url = f"https://youtu.be/{i}"
        ledger.create_job(url, "video")
        if status != "PENDING":
            ledger.mark(url, "video", status, error="x" if status == "FAILED" else None)
    stats = ledger.stats()
    assert stats["PENDING"] == 2
    assert stats["DOWNLOADING"] == 1
    assert stats["COMPLETED"] == 1
    assert stats["FAILED"] == 1
    assert stats["TOTAL"] == 5
    assert len(ledger.jobs("COMPLETED")) == 1
    assert len(ledger.pending()) == 2


def test_artifacts_and_events_roundtrip(ledger):
    job = ledger.create_job("https://youtu.be/f", "audio")
    art = ledger.record_artifact(job.id, "staged", "/staging/f.webm", bytes_written=99,
                                 sha256="abc", status="VALIDATED")
    ledger.update_artifact(art, status="FINAL", path="/out/f.flac", sha256="def", bytes_written=120)
    rows = ledger.artifacts(job.id)
    assert len(rows) == 1
    assert rows[0]["status"] == "FINAL"
    assert rows[0]["sha256"] == "def"
    assert rows[0]["bytes"] == 120

    ledger.log("staged f.webm", level="info", job_id=job.id, data={"bytes": 99})
    events = ledger.events(5)
    assert events[-1]["message"] == "staged f.webm"
    assert '"bytes": 99' in events[-1]["data"]


def test_playlist_index_queries(ledger):
    for idx in (3, 1, 2):
        ledger.create_job(f"https://youtu.be/p{idx}", "video", playlist_id="PL1", playlist_index=idx)
    ordered = ledger.retryable(["video"])
    assert [j.playlist_index for j in ordered] == [1, 2, 3]


def test_concurrent_marks_stay_consistent(ledger):
    url = "https://youtu.be/t"
    ledger.create_job(url, "video")
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(50):
                ledger.mark(url, "video", "DOWNLOADING", bump_attempts=True)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert ledger.get_job(url, "video").attempts == 200
