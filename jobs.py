"""Background job lifecycle for Telegram Turntable.

Every job kind (sync, preview, source counts, prefetch) used to copy the same
running→complete/cancelled/error skeleton and the same dedup lookup, and three different
task registries had to be audited on shutdown. The runner owns the lifecycle once: start()
wraps the work coroutine in the state machine, active()/cancel() express dedup and
replacement, and cancel_all() is the single shutdown path. The public() JSON contract is
what the frontend polls, so it is frozen here.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

JOB_RETENTION_SECONDS = 15 * 60
JOB_MAX_SURVIVORS = 100


@dataclass
class BackgroundJob:
    id: str
    kind: str
    chat_id: str | None = None
    mode: str | None = None
    state: str = "queued"
    processed: int = 0
    found: int = 0
    error: str = ""
    result: Any = None
    created_at: float = field(default_factory=time.monotonic)
    task: asyncio.Task[Any] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "jobId": self.id,
            "kind": self.kind,
            "chatId": self.chat_id or None,
            "mode": self.mode or None,
            "state": self.state,
            "processed": self.processed,
            "found": self.found,
            "error": self.error or None,
            "result": self.result,
        }


class JobRunner:
    def __init__(self) -> None:
        self.jobs: dict[str, BackgroundJob] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coroutine: Any) -> None:
        """Fire-and-forget work with no status surface (e.g. the startup sync-all)."""
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def start(
        self,
        job: BackgroundJob,
        work: Awaitable[Any],
        error_mapper: Callable[[Exception], str] | None = None,
    ) -> dict[str, Any]:
        self.prune()
        self.jobs[job.id] = job
        task = asyncio.create_task(self._execute(job, work, error_mapper))
        job.task = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return job.public()

    def register(self, job: BackgroundJob) -> dict[str, Any]:
        """Insert an already-terminal job (e.g. an empty prefetch) without running it."""
        self.prune()
        self.jobs[job.id] = job
        return job.public()

    async def _execute(
        self,
        job: BackgroundJob,
        work: Awaitable[Any],
        error_mapper: Callable[[Exception], str] | None,
    ) -> None:
        job.state = "running"
        try:
            await work
            job.state = "complete"
        except asyncio.CancelledError:
            job.state = "cancelled"
        except Exception as error:
            job.error = (error_mapper or self._default_error)(error)
            job.state = "error"

    @staticmethod
    def _default_error(error: Exception) -> str:
        return str(error)

    def active(self, kind: str, chat_id: str | None = None) -> BackgroundJob | None:
        """The running job of *kind* (optionally for one chat), for dedup and replacement."""
        return next(
            (
                job for job in self.jobs.values()
                if job.kind == kind
                and (chat_id is None or job.chat_id == chat_id)
                and job.state in {"queued", "running"}
            ),
            None,
        )

    def status(self, job_id: str) -> dict[str, Any]:
        self.prune()
        job = self.jobs.get(job_id)
        if not job:
            raise KeyError("Background job not found")
        return job.public()

    def cancel_by_id(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if not job:
            raise KeyError("Background job not found")
        if job.state in {"queued", "running"} and job.task:
            job.task.cancel()
            if job.state == "queued":
                # The task never ran a tick, so its handler will not run to record the
                # cancellation; without this it would sit "queued" forever and dedup would
                # keep mistaking it for an active job.
                job.state = "cancelled"
        return job.public()

    def cancel(self, kind: str, chat_id: str | None = None) -> BackgroundJob | None:
        """Cancel the running job of *kind* -- the replacement rule prefetch relies on."""
        job = self.active(kind, chat_id)
        if job and job.task:
            job.task.cancel()
            if job.state == "queued":
                job.state = "cancelled"
        return job

    def prune(self) -> None:
        now = time.monotonic()
        terminal = [
            job for job in self.jobs.values()
            if job.state not in {"queued", "running"}
        ]
        remove = {job.id for job in terminal if now - job.created_at > JOB_RETENTION_SECONDS}
        survivors = sorted(
            (job for job in terminal if job.id not in remove),
            key=lambda job: job.created_at,
            reverse=True,
        )
        remove.update(job.id for job in survivors[JOB_MAX_SURVIVORS:])
        for job_id in remove:
            self.jobs.pop(job_id, None)

    async def cancel_all(self) -> None:
        """The single shutdown path: every task this runner spawned, cancelled."""
        for task in list(self._background_tasks):
            task.cancel()
        await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
