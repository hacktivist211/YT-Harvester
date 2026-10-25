"""Bounded asynchronous worker queues.

Three isolated stages with independent concurrency budgets:

* ``download``  - network fetchers (yt-dlp / HTTP)
* ``assemble``  - staging validation, checksums, atomic moves, tagging prep
* ``transcode`` - FFmpeg remux / transcode jobs

Each stage owns a bounded ``asyncio.Queue`` plus N worker tasks, so a runaway
playlist can never open more sockets or ffmpeg processes than configured.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

Stage = str
STAGES: tuple[Stage, ...] = ("download", "assemble", "transcode")

Fn = Callable[..., Any]


@dataclass
class StageStats:
    queue_size: int = 0
    workers: int = 0
    submitted: int = 0
    completed: int = 0
    failed: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "queue_size": self.queue_size,
            "workers": self.workers,
            "submitted": self.submitted,
            "completed": self.completed,
            "failed": self.failed,
        }


@dataclass
class _Item:
    fn: Fn
    args: tuple
    kwargs: dict
    future: asyncio.Future
    stage: Stage


class WorkerQueues:
    """Run blocking callables on per-stage worker pools with bounded queues."""

    def __init__(
        self,
        downloaders: int = 2,
        assemblers: int = 1,
        transcoders: int = 1,
        queue_size: int = 8,
    ) -> None:
        budgets = {"download": downloaders, "assemble": assemblers, "transcode": transcoders}
        for stage, count in budgets.items():
            if count < 1:
                raise ValueError(f"{stage} pool size must be >= 1")
        if queue_size < 1:
            raise ValueError("queue_size must be >= 1")
        self.budgets = budgets
        self.queue_size = queue_size
        self.queues: dict[Stage, asyncio.Queue] = {}
        self.stats: dict[Stage, StageStats] = {}
        self._workers: dict[Stage, list[asyncio.Task]] = {}
        self._started = False

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> "WorkerQueues":
        if self._started:
            return self
        for stage in STAGES:
            self.queues[stage] = asyncio.Queue(maxsize=self.queue_size)
            self.stats[stage] = StageStats(workers=self.budgets[stage])
            self._workers[stage] = [
                asyncio.create_task(self._worker(stage), name=f"ytharvest-{stage}-{i}")
                for i in range(self.budgets[stage])
            ]
        self._started = True
        return self

    async def stop(self, cancel: bool = True) -> None:
        for tasks in self._workers.values():
            for task in tasks:
                if cancel:
                    task.cancel()
        for tasks in self._workers.values():
            await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        self._started = False

    async def __aenter__(self) -> "WorkerQueues":
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ----------------------------------------------------------------- API
    async def submit(self, stage: Stage, fn: Fn, *args: Any, **kwargs: Any) -> Any:
        """Schedule ``fn`` on ``stage`` and await its completion."""
        if not self._started:
            await self.start()
        if stage not in self.queues:
            raise KeyError(f"Unknown stage '{stage}' (expected one of {STAGES})")
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        item = _Item(fn=fn, args=args, kwargs=kwargs, future=future, stage=stage)
        self.stats[stage].submitted += 1
        await self.queues[stage].put(item)
        return await future

    async def submit_many(
        self, stage: Stage, jobs: list[tuple[Fn, tuple, dict]]
    ) -> list[Any]:
        tasks = [asyncio.create_task(self.submit(stage, fn, *a, **kw)) for fn, a, kw in jobs]
        return await asyncio.gather(*tasks)

    async def drain(self) -> None:
        """Wait until every queue is empty and no worker is busy."""
        if not self._started:
            return
        await asyncio.gather(*(q.join() for q in self.queues.values()))

    def snapshot(self) -> dict[str, dict[str, int]]:
        snap: dict[str, dict[str, int]] = {}
        for stage, stats in self.stats.items():
            data = stats.to_dict()
            data["queue_size"] = self.queues[stage].qsize() if stage in self.queues else 0
            snap[stage] = data
        return snap

    # -------------------------------------------------------------- worker
    async def _worker(self, stage: Stage) -> None:
        queue = self.queues[stage]
        stats = self.stats[stage]
        while True:
            item: _Item = await queue.get()
            try:
                if item.future.cancelled():
                    continue
                result = await asyncio.to_thread(item.fn, *item.args, **item.kwargs)
            except Exception as exc:  # noqa: BLE001 - surfaced through the future
                stats.failed += 1
                if not item.future.done():
                    item.future.set_exception(exc)
                else:  # pragma: no cover
                    pass
            else:
                stats.completed += 1
                if not item.future.done():
                    item.future.set_result(result)
            finally:
                queue.task_done()


async def run_pipeline(
    stage_plan: list[tuple[Stage, list[tuple[Fn, tuple, dict]]]],
    **budgets: int,
) -> dict[str, Any]:
    """Convenience helper: run ``[(stage, [callable tuples]), ...]`` through pools."""
    pool = WorkerQueues(**budgets)
    results: dict[str, list[Any]] = {}
    async with pool:
        for stage, jobs in stage_plan:
            results[stage] = await pool.submit_many(stage, jobs)
        await pool.drain()
    return {"results": results, "stats": pool.snapshot()}
