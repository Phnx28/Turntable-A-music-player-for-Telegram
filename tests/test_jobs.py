import asyncio
import time
import unittest

from jobs import BackgroundJob, JobRunner, JOB_MAX_SURVIVORS, JOB_RETENTION_SECONDS


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
        # time.monotonic() is boot-relative and large, so a created_at of 0 is ancient.
        old = self._job()
        old.state = "complete"
        old.created_at = 0
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
