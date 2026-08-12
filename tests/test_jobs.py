import asyncio
import logging
import time
import unittest

from jobs import BackgroundJob, JobRunner, LOGGER, JOB_MAX_SURVIVORS, JOB_RETENTION_SECONDS


class JobRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runner = JobRunner()
        self._next_id = 0

    def _job(self, kind="sync", chat_id="1", result=None):
        self._next_id += 1
        return BackgroundJob(f"job-{self._next_id}", kind, chat_id=chat_id, mode="full", result=result)

    def _work(self, job, *, fail=None, hold=None):
        async def run():
            if hold:
                await hold.wait()
            if fail:
                raise fail
            job.processed += 1
        return run()

    async def test_complete_path(self):
        job = self._job()
        hold = asyncio.Event()
        self.runner.start(job, self._work(job, hold=hold))
        await asyncio.sleep(0)
        self.assertEqual("running", job.state)
        hold.set()
        await job.task
        self.assertEqual("complete", job.state)
        self.assertEqual(1, job.processed)
        self.assertEqual(job.public(), self.runner.status(job.id))

    async def test_error_path_uses_the_injected_mapper(self):
        job = self._job()
        self.runner.start(job, self._work(job, fail=RuntimeError("boom")), error_mapper=lambda error: f"mapped: {error}")
        await job.task
        self.assertEqual("error", job.state)
        self.assertEqual("mapped: boom", job.error)

    async def test_cancelled_path(self):
        job = self._job()
        hold = asyncio.Event()
        self.runner.start(job, self._work(job, hold=hold))
        await asyncio.sleep(0)
        self.runner.cancel_by_id(job.id)
        await job.task
        self.assertEqual("cancelled", job.state)

    async def test_cancel_before_the_task_starts_records_cancelled(self):
        # A job cancelled on the same tick as start() never runs its handler; the runner must
        # not leave it "queued", or the dedup would keep treating it as active.
        job = self._job(kind="prefetch")
        self.runner.start(job, self._work(job))
        self.runner.cancel("prefetch")
        self.assertEqual("cancelled", job.state)
        self.assertIsNone(self.runner.active("prefetch"))

    async def test_active_dedupes_by_kind_and_chat(self):
        job = self._job(kind="sync", chat_id="1")
        hold = asyncio.Event()
        self.runner.start(job, self._work(job, hold=hold))
        await asyncio.sleep(0)
        self.assertIs(self.runner.active("sync", "1"), job)
        self.assertIsNone(self.runner.active("sync", "2"), "a different chat is not the same sync")
        self.assertIsNone(self.runner.active("preview", "1"), "a different kind is not the same job")
        self.runner.cancel("sync", "1")
        await job.task
        self.assertIsNone(self.runner.active("sync", "1"), "a cancelled job is no longer active")

    async def test_cancel_replaces_a_running_prefetch(self):
        first = self._job(kind="prefetch")
        hold = asyncio.Event()
        self.runner.start(first, self._work(first, hold=hold))
        await asyncio.sleep(0)
        self.runner.cancel("prefetch")
        await first.task
        self.assertEqual("cancelled", first.state)
        self.assertIsNone(self.runner.active("prefetch"))

    async def test_register_keeps_a_terminal_job_pollable(self):
        job = self._job(kind="prefetch", result={})
        job.state = "complete"
        public = self.runner.register(job)
        self.assertEqual("complete", public["state"])
        self.assertIs(self.runner.jobs[job.id], job)

    def test_unknown_job_status_raises(self):
        with self.assertRaises(KeyError):
            self.runner.status("missing")

    def test_prune_drops_old_terminal_jobs_and_caps_survivors(self):
        # time.monotonic() is boot-relative and large, so a finished_at of 0 is ancient.
        old = self._job()
        old.state = "complete"
        old.created_at = 0
        old.finished_at = 0
        self.runner.register(old)
        self.runner.prune()
        self.assertNotIn(old.id, self.runner.jobs)

        # More than the cap of survivors: the oldest ones go.
        now = time.monotonic()
        for index in range(JOB_MAX_SURVIVORS + 5):
            job = self._job(chat_id=str(index))
            job.state = "complete"
            job.created_at = now + index  # recent enough to survive the age check
            self.runner.register(job)
        self.runner.prune()
        self.assertEqual(JOB_MAX_SURVIVORS, len(self.runner.jobs))

    async def test_long_running_job_keeps_full_retention_after_finish(self):
        # A job that started beyond the retention window must survive a full retention
        # window once it finishes, and only be pruned when finished_at itself ages out.
        job = self._job()
        job.created_at = 0  # ancient: already past retention while it was queued
        hold = asyncio.Event()
        self.runner.start(job, self._work(job, hold=hold))
        await asyncio.sleep(0)
        hold.set()
        await job.task
        self.assertEqual("complete", job.state)
        self.assertIsNotNone(job.finished_at)
        self.runner.prune()
        self.assertIn(job.id, self.runner.jobs, "a just-finished job survives the full window")
        job.finished_at = 0  # now ancient too
        self.runner.prune()
        self.assertNotIn(job.id, self.runner.jobs, "an aged terminal job is pruned")

    def test_register_stamps_finished_at_on_terminal_job(self):
        job = self._job(kind="prefetch", result={})
        job.state = "complete"
        self.runner.register(job)
        self.assertIsNotNone(job.finished_at)

    async def test_cancelling_a_queued_job_stamps_finished_at(self):
        # A queued task never runs its handler, so the runner itself must record the
        # cancellation timestamp for both cancel entry points.
        by_id = self._job(kind="prefetch")
        self.runner.start(by_id, self._work(by_id))
        self.runner.cancel_by_id(by_id.id)
        self.assertEqual("cancelled", by_id.state)
        self.assertIsNotNone(by_id.finished_at)

        by_kind = self._job(kind="prefetch")
        self.runner.start(by_kind, self._work(by_kind))
        self.runner.cancel("prefetch")
        self.assertEqual("cancelled", by_kind.state)
        self.assertIsNotNone(by_kind.finished_at)

    async def test_spawn_consumes_and_logs_task_exceptions(self):
        async def boom():
            raise RuntimeError("spawned boom")

        # Watch the asyncio logger for the unretrieved-exception warning: it must never
        # fire, because the runner retrieves the exception in its done callback.
        asyncio_logger = logging.getLogger("asyncio")
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        previous_level = asyncio_logger.level
        asyncio_logger.setLevel(logging.WARNING)
        asyncio_logger.addHandler(handler)
        try:
            with self.assertLogs(LOGGER, level="ERROR") as captured:
                self.runner.spawn(boom())
                await asyncio.gather(*list(self.runner._background_tasks), return_exceptions=True)
                await asyncio.sleep(0)  # let the done callbacks run
        finally:
            asyncio_logger.removeHandler(handler)
            asyncio_logger.setLevel(previous_level)

        self.assertTrue(
            any("spawned boom" in line for line in captured.output),
            "the spawned failure must be logged",
        )
        self.assertFalse(
            any("Task exception was never retrieved" in line for line in records),
            "a retrieved spawn failure must not warn",
        )

    async def test_cancel_all_is_the_single_shutdown_path(self):
        job = self._job()
        hold = asyncio.Event()
        self.runner.start(job, self._work(job, hold=hold))
        spawned = asyncio.Event()
        self.runner.spawn(self._work(self._job(), hold=spawned))
        await asyncio.sleep(0)
        await self.runner.cancel_all()
        self.assertEqual("cancelled", job.state)
        self.assertEqual([], list(self.runner._background_tasks))


if __name__ == "__main__":
    unittest.main()
