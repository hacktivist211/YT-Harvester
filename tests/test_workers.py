"""Bounded async worker queues: budgets, isolation and failure propagation."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from ytharvest.workers.queues import STAGES, WorkerQueues, run_pipeline


def test_stage_names_are_isolated():
    assert STAGES == ("download", "assemble", "transcode")


def test_invalid_budgets_rejected():
    with pytest.raises(ValueError):
        WorkerQueues(downloaders=0)
    with pytest.raises(ValueError):
        WorkerQueues(transcoders=0, queue_size=0)


def test_concurrency_never_exceeds_the_budget():
    live = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def work(i: int) -> int:
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        time.sleep(0.05)
        with lock:
            live["now"] -= 1
        return i

    async def main() -> list[int]:
        pool = WorkerQueues(downloaders=2, assemblers=1, transcoders=1, queue_size=4)
        async with pool:
            results = await asyncio.gather(
                *(pool.submit("download", work, i) for i in range(8))
            )
            await pool.drain()
            return results

    results = asyncio.run(main())
    assert sorted(results) == list(range(8))
    assert live["peak"] <= 2                          # budget respected


def test_each_stage_uses_its_own_budget():
    seen: dict[str, list[int]] = {"download": [], "assemble": [], "transcode": []}

    def make(stage: str):
        def fn(i: int) -> int:
            seen[stage].append(i)
            time.sleep(0.01)
            return i
        return fn

    async def main() -> None:
        pool = WorkerQueues(downloaders=2, assemblers=1, transcoders=1, queue_size=8)
        async with pool:
            await asyncio.gather(
                pool.submit("download", make("download"), 1),
                pool.submit("assemble", make("assemble"), 2),
                pool.submit("transcode", make("transcode"), 3),
            )
            await pool.drain()

    asyncio.run(main())
    assert seen["download"] == [1]
    assert seen["assemble"] == [2]
    assert seen["transcode"] == [3]


def test_exceptions_propagate_to_the_submitter():
    def boom() -> None:
        raise ValueError("ffmpeg exploded")

    async def main() -> None:
        pool = WorkerQueues(downloaders=1)
        async with pool:
            with pytest.raises(ValueError, match="ffmpeg exploded"):
                await pool.submit("transcode", boom)

    asyncio.run(main())


def test_stats_track_submitted_completed_failed():
    async def main() -> dict:
        pool = WorkerQueues(downloaders=1, assemblers=1, transcoders=1, queue_size=8)
        async with pool:
            await pool.submit("download", lambda: 1)
            try:
                await pool.submit("assemble", lambda: 1 / 0)
            except ZeroDivisionError:
                pass
            await pool.drain()
            return pool.snapshot()

    snapshot = asyncio.run(main())
    assert snapshot["download"]["completed"] == 1
    assert snapshot["assemble"]["failed"] == 1
    assert snapshot["download"]["submitted"] == 1
    assert snapshot["transcode"]["queue_size"] == 0


def test_submit_many_preserves_order():
    async def main() -> list[int]:
        pool = WorkerQueues(downloaders=4, queue_size=16)
        async with pool:
            jobs = [(lambda i=i: i, (), {}) for i in range(10)]
            return await pool.submit_many("download", jobs)

    assert asyncio.run(main()) == list(range(10))


def test_unknown_stage_rejected():
    async def main() -> None:
        pool = WorkerQueues()
        async with pool:
            with pytest.raises(KeyError):
                await pool.submit("nonsense", lambda: None)

    asyncio.run(main())


def test_run_pipeline_helper():
    outcome = asyncio.run(
        run_pipeline(
            [
                ("download", [(lambda: "raw", (), {})]),
                ("transcode", [(lambda: "remuxed", (), {})]),
            ],
            downloaders=1,
            transcoders=1,
        )
    )
    assert outcome["results"]["download"] == ["raw"]
    assert outcome["results"]["transcode"] == ["remuxed"]
    assert outcome["stats"]["download"]["completed"] == 1


def test_submit_after_start_is_allowed():
    async def main() -> int:
        pool = WorkerQueues(downloaders=1)
        await pool.start()
        value = await pool.submit("download", lambda: 42)
        await pool.stop()
        return value

    assert asyncio.run(main()) == 42
