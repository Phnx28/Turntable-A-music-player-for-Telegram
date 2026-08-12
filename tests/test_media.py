import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import Database, media_digest, media_identity, parse_range_header
from media import MediaCache, MediaSource, MEDIA_CHUNK_SIZE


class _FakeClient:
    """The in-test adapter for the Media cache: canned documents and chunk streams.

    MediaCache takes a client provider, so no Telethon object ever reaches these tests.
    """

    def __init__(self, messages=None, chunk_stream=None):
        self.messages = messages or {}
        self.chunk_stream = chunk_stream or (lambda document, start, length: iter([]))
        self.get_messages_calls = 0

    async def get_messages(self, chat_id, ids=None):
        self.get_messages_calls += 1
        return self.messages.get((str(chat_id), str(ids)))

    def iter_download(self, document, offset=0, limit=0, chunk_size=0, request_size=0, file_size=0):
        return self.chunk_stream(document, offset, limit, file_size)


def _stall_stream(document, start, length, file_size):
    async def stream():
        await asyncio.sleep(3600)
        yield b""
    return stream()


def _chunk_stream(document, start, length, file_size):
    async def stream():
        remaining = file_size - start
        while remaining > 0:
            size = min(MEDIA_CHUNK_SIZE, remaining)
            yield b"z" * size
            remaining -= size
    return stream()


def _gated_stream(document, start, length, file_size, gate, release):
    """Signal *gate* before the first chunk, then hold until *release*."""
    async def stream():
        remaining = file_size - start
        first = True
        while remaining > 0:
            size = min(MEDIA_CHUNK_SIZE, remaining)
            if first:
                first = False
                gate.set()
                await release.wait()
            yield b"z" * size
            remaining -= size
    return stream()


def _capturing_stream(captured, payload=b"z"):
    """Record (start, length) of every download; used to prove resume offsets."""
    async def stream(document, start, length, file_size):
        captured.append((start, length))
        remaining = file_size - start
        while remaining > 0:
            size = min(MEDIA_CHUNK_SIZE, remaining)
            yield payload * size
            remaining -= size
    return stream


class MediaCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database = Database(Path(self._tmp.name) / "library.sqlite3")
        self.addCleanup(self.database.close)
        self.database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
        self.database.upsert_tracks([{
            "chatId": "1", "messageId": "2", "fileName": "song.mp3", "mimeType": "audio/mpeg",
            "fileSize": 1000, "title": "Track", "artist": "Artist", "documentId": "9",
        }])
        self.root = Path(self._tmp.name)
        self.client = _FakeClient()
        self.media = MediaCache(
            self.database,
            media_directory=self.root / "media-cache",
            download_directory=self.root / "tagged-downloads",
            client_provider=lambda: self.client,
        )

    @property
    def track(self):
        return {
            "key": "1:2", "chatId": "1", "messageId": "2", "documentId": "9",
            "file": {"name": "song.mp3", "size": 1000},
        }

    def _seed_document(self):
        self.client.messages[("1", "2")] = SimpleNamespace(
            document=SimpleNamespace(id=9, size=1000)
        )

    def _seed_cache_file(self, name="audio", size=1000):
        identity = media_identity("9", size)
        digest = media_digest("1:2", identity)
        destination = self.media.media_directory / f"{digest}.{name}"
        destination.write_bytes(b"x" * size)
        if name == "audio":
            self.database.save_media_cache("1:2", identity, destination.name, size)
        return destination

    def _seed_partial(self, size=600):
        identity = media_identity("9", 1000)
        digest = media_digest("1:2", identity)
        partial = self.media.media_directory / f"{digest}.part"
        partial.write_bytes(b"p" * size)
        return partial

    async def test_cold_miss_fetches_the_document_exactly_once(self):
        self._seed_document()
        self.client.chunk_stream = _chunk_stream
        source = await self.media.media_source(self.track)
        self.assertIsInstance(source, MediaSource)
        self.assertEqual(1000, source.size)
        body = b"".join([chunk async for chunk in source.iter_range(parse_range_header(None, 1000))])
        self.assertEqual(1000, len(body))
        self.assertEqual(1, self.client.get_messages_calls)

    async def test_complete_cache_serves_without_any_telegram_call(self):
        self._seed_cache_file()
        source = await self.media.media_source(self.track)
        self.assertEqual(1000, source.size)
        body = b"".join([chunk async for chunk in source.iter_range(parse_range_header("bytes=100-199", 1000))])
        self.assertEqual(b"x" * 100, body)
        self.assertEqual(0, self.client.get_messages_calls)

    async def test_partial_file_serves_only_covered_ranges(self):
        self._seed_partial(size=600)
        covered = await self.media.media_source(self.track)
        body = b"".join([chunk async for chunk in covered.iter_range(parse_range_header("bytes=0-499", 1000))])
        self.assertEqual(b"p" * 500, body)
        self.assertEqual(0, self.client.get_messages_calls, "covered ranges must not touch Telegram")

        self._seed_document()
        self.client.chunk_stream = _chunk_stream
        uncovered = await self.media.media_source(self.track)
        body = b"".join([chunk async for chunk in uncovered.iter_range(parse_range_header("bytes=700-799", 1000))])
        self.assertEqual(100, len(body))
        self.assertEqual(1, self.client.get_messages_calls, "uncovered ranges fall back to Telegram")

    async def test_partial_file_reports_the_total_size_not_the_downloaded_part(self):
        self._seed_partial(size=600)
        source = await self.media.media_source(self.track)
        self.assertEqual(1000, source.size, "range/416 math needs the real total, not 600")

    async def test_iter_media_times_out_instead_of_hanging(self):
        self.client.chunk_stream = _stall_stream
        with patch("media.TELEGRAM_CHUNK_TIMEOUT_SECONDS", 0.05):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                async for _ in self.media.iter_media(SimpleNamespace(id=9, size=1000), 0, 1000):
                    pass

    async def test_get_message_document_marks_missing_media_unavailable(self):
        self.client.messages[("1", "2")] = SimpleNamespace(document=None)
        with self.assertRaises(KeyError):
            await self.media.get_message_document("1", "2")
        self.assertEqual(0, self.database.get_track("1", "2")["available"])

    async def test_eviction_respects_budget_protection_and_partial_grace(self):
        self._seed_cache_file()
        identity = media_identity("9", 1000)
        digest = media_digest("1:2", identity)
        paths = {}
        for index, size in ((3, 100), (4, 100)):
            self.database.upsert_tracks([{
                "chatId": "1", "messageId": str(index), "fileName": f"song-{index}.mp3",
                "mimeType": "audio/mpeg", "fileSize": size, "title": f"T{index}", "artist": "A",
                "documentId": str(index),
            }])
            entry_identity = media_identity(str(index), size)
            entry_digest = media_digest(f"1:{index}", entry_identity)
            path = self.media.media_directory / f"{entry_digest}.audio"
            path.write_bytes(b"x" * size)
            self.database.save_media_cache(f"1:{index}", entry_identity, path.name, size)
            paths[index] = path
        self.media.protected_keys = {"1:2", "1:3"}
        old_partial = self._seed_partial(size=200)
        os.utime(old_partial, (0, 0))
        fresh_partial = self.media.media_directory / f"{digest}.fresh.part"
        fresh_partial.write_bytes(b"y" * 200)

        # Budget: enough for the protected tracks (1100) plus the fresh partial (200); the
        # unprotected entry and the stale partial must go.
        self.media._evict_cache_sync(maximum=1300)
        self.assertTrue((self.media.media_directory / f"{digest}.audio").exists(), "protected entry kept")
        self.assertTrue(paths[3].exists(), "protected entry kept")
        self.assertFalse(paths[4].exists(), "unprotected entry evicted")
        self.assertTrue(fresh_partial.exists(), "actively recent partial kept")
        self.assertFalse(old_partial.exists(), "stale partial evicted")

    def test_clean_partial_cache_removes_stale_and_empty_files(self):
        identity = media_identity("9", 1000)
        digest = media_digest("1:2", identity)
        stale = self.media.media_directory / f"{digest}.stale.part"
        stale.write_bytes(b"old")
        os.utime(stale, (0, 0))
        empty = self.media.media_directory / f"{digest}.empty.part"
        empty.touch()
        fresh = self.media.media_directory / f"{digest}.fresh.part"
        fresh.write_bytes(b"new")
        self.media.clean_partial_cache()
        self.assertFalse(stale.exists())
        self.assertFalse(empty.exists())
        self.assertTrue(fresh.exists())

    async def test_clear_cache_removes_everything(self):
        self._seed_cache_file()
        self._seed_partial()
        self.client.messages[("1", "2")] = SimpleNamespace(document=None)
        result = await self.media.clear_all()
        self.assertGreaterEqual(result["removedBytes"], 1600)
        self.assertEqual([], list(self.media.media_directory.iterdir()))

    def _seed_second_track(self, message_id="3", document_id="3", size=1000):
        self.database.upsert_tracks([{
            "chatId": "1", "messageId": message_id, "fileName": f"song-{message_id}.mp3",
            "mimeType": "audio/mpeg", "fileSize": size, "title": f"T{message_id}",
            "artist": "Artist", "documentId": document_id,
        }])
        self.client.messages[("1", message_id)] = SimpleNamespace(
            document=SimpleNamespace(id=int(document_id), size=size)
        )


class CacheConcurrencyTests(MediaCacheTests):
    """B1/B2: keyed locks let different tracks download together; the same track never twice."""

    async def test_different_tracks_download_concurrently(self):
        self._seed_document()
        self._seed_second_track()
        gate, release = asyncio.Event(), asyncio.Event()
        self.client.chunk_stream = lambda document, start, length, file_size: _gated_stream(
            document, start, length, file_size, gate, release
        )
        first = asyncio.create_task(self.media.cache_media(self.track))
        second = asyncio.create_task(self.media.cache_media(
            {"key": "1:3", "chatId": "1", "messageId": "3", "documentId": "3",
             "file": {"name": "song-3.mp3", "size": 1000}}
        ))
        await asyncio.wait_for(gate.wait(), 5)
        # Both downloads are mid-stream at the same time: with a global cache lock the
        # second could not even have fetched its document yet.
        self.assertEqual(2, self.client.get_messages_calls)
        release.set()
        await asyncio.gather(first, second)
        self.assertTrue((self.media.media_directory / f"{media_digest('1:2', media_identity('9', 1000))}.audio").exists())
        self.assertTrue((self.media.media_directory / f"{media_digest('1:3', media_identity('3', 1000))}.audio").exists())

    async def test_same_track_requested_twice_downloads_once(self):
        self._seed_document()
        gate, release = asyncio.Event(), asyncio.Event()
        self.client.chunk_stream = lambda document, start, length, file_size: _gated_stream(
            document, start, length, file_size, gate, release
        )
        first = asyncio.create_task(self.media.cache_media(self.track))
        await asyncio.wait_for(gate.wait(), 5)
        second = asyncio.create_task(self.media.cache_media(self.track))
        release.set()
        first_path, second_path = await asyncio.gather(first, second)
        self.assertEqual(first_path, second_path)
        self.assertEqual(1, self.client.get_messages_calls, "the second caller must reuse the first download")

    async def test_start_cache_current_deduplicates_active_tasks(self):
        self._seed_document()
        gate, release = asyncio.Event(), asyncio.Event()
        self.client.chunk_stream = lambda document, start, length, file_size: _gated_stream(
            document, start, length, file_size, gate, release
        )
        self.media.start_cache_current("1:2")
        self.media.start_cache_current("1:2")
        self.assertEqual(1, len(self.media.active_cache_tasks), "a second request must reuse the active task")
        release.set()
        await asyncio.wait_for(self.media.active_cache_tasks["1:2"], 5)
        self.assertEqual(1, self.client.get_messages_calls)
        self.assertNotIn("1:2", self.media.active_cache_tasks, "finished tasks leave the map")


class CacheClearConcurrencyTests(MediaCacheTests):
    """B3: clear_all cancels and awaits writers before deleting files."""

    async def test_clear_during_active_download_is_safe(self):
        self._seed_document()
        self._seed_second_track()
        gate, release = asyncio.Event(), asyncio.Event()
        # Track 1:2 is held mid-download; track 1:3 completes fully.
        def stream_for(document, start, length, file_size):
            if document.id == 3:
                return _chunk_stream(document, start, length, file_size)
            return _gated_stream(document, start, length, file_size, gate, release)
        self.client.chunk_stream = stream_for
        self.media.start_cache_current("1:2")
        await asyncio.wait_for(gate.wait(), 5)
        self.media.start_cache_current("1:3")
        for _ in range(100):
            if (self.media.media_directory / f"{media_digest('1:3', media_identity('3', 1000))}.audio").exists():
                break
            await asyncio.sleep(0.01)
        cleared = await self.media.clear_all()
        self.assertGreaterEqual(cleared["removedBytes"], 1000)
        self.assertEqual([], list(self.media.media_directory.glob("*.part")))
        self.assertEqual([], list(self.media.media_directory.glob("*.audio")))
        self.assertEqual([], self.database.media_cache_entries())
        release.set()
        await asyncio.sleep(0.05)
        # The cancelled writer must not resume and recreate its .part.
        self.assertEqual([], list(self.media.media_directory.glob("*.part")))
        # Later caching works normally.
        self._seed_second_track(message_id="4", document_id="4", size=500)
        path = await self.media.cache_media({
            "key": "1:4", "chatId": "1", "messageId": "4", "documentId": "4",
            "file": {"name": "song-4.mp3", "size": 500},
        })
        self.assertTrue(path.is_file())


class PartialReaderTests(MediaCacheTests):
    """B4: a .part file is never truncated while a response is reading it."""

    def _digest(self):
        return media_digest("1:2", media_identity("9", 1000))

    async def test_iter_range_registers_and_releases_the_reader(self):
        self._seed_partial(size=600)
        source = await self.media.media_source(self.track)
        self.assertEqual({}, self.media._active_readers)
        body = b"".join([chunk async for chunk in source.iter_range(parse_range_header("bytes=0-499", 1000))])
        self.assertEqual(b"p" * 500, body)
        self.assertEqual({}, self.media._active_readers, "reader must be released after streaming")
        self.assertEqual(0, self.client.get_messages_calls)

    async def test_resume_defers_realignment_while_a_reader_is_active(self):
        self._seed_document()
        self._seed_partial(size=600)  # 600 is not chunk-aligned: a resume would truncate it
        captured = []
        self.client.chunk_stream = _capturing_stream(captured)
        self.media._register_reader(self._digest())
        try:
            path = await self.media.cache_media(self.track)
        finally:
            self.media._unregister_reader(self._digest())
        self.assertEqual(600, captured[0][0], "with a reader active the resume must append, not truncate")
        self.assertTrue(path.is_file())
        # A later resume without readers realigns (truncates to 0) as before.
        self.database.delete_media_cache(["1:2"])
        (self.media.media_directory / f"{self._digest()}.audio").unlink(missing_ok=True)
        self._seed_partial(size=600)
        captured.clear()
        await self.media.cache_media(self.track)
        self.assertEqual(0, captured[0][0], "without readers the alignment truncation still applies")


class CacheBookkeepingTests(MediaCacheTests):
    """B6: the aggregate counter lets eviction skip the full scan when under budget."""

    async def test_counter_tracks_finalize_evict_and_clear(self):
        self._seed_cache_file()
        self.assertIsNone(self.media._cache_bytes)
        self.media._evict_cache_sync(maximum=5 * 1024 * 1024 * 1024)
        self.assertEqual(1000, self.media._cache_bytes, "first eviction builds the counter")

        self._seed_second_track(message_id="3", document_id="3", size=100)
        self.client.messages[("1", "3")] = SimpleNamespace(document=SimpleNamespace(id=3, size=100))
        self.client.chunk_stream = _chunk_stream
        path = await self.media.cache_media({
            "key": "1:3", "chatId": "1", "messageId": "3", "documentId": "3",
            "file": {"name": "song-3.mp3", "size": 100},
        })
        self.assertTrue(path.is_file())
        self.assertEqual(1100, self.media._cache_bytes, "finalize must add the new entry")

        self.media._evict_cache_sync(maximum=1050)
        # Entries evict oldest last_accessed first: the 1000-byte seed goes, the 100-byte
        # entry survives.
        self.assertEqual(100, self.media._cache_bytes, "eviction must subtract the victims")

        await self.media.clear_all()
        self.assertEqual(0, self.media._cache_bytes, "clear must reset the counter")

    def test_under_budget_eviction_skips_the_full_scan(self):
        self._seed_cache_file()
        self.media._cache_bytes = 1000
        scanned = []

        def explode():
            scanned.append(1)
            raise AssertionError("under-budget eviction must not rescan the cache")

        original = self.media._scan_cache_bytes_sync
        self.media._scan_cache_bytes_sync = explode
        try:
            self.media._evict_cache_sync(maximum=5 * 1024 * 1024 * 1024)
        finally:
            self.media._scan_cache_bytes_sync = original
        self.assertEqual([], scanned)
        self.assertTrue((self.media.media_directory / f"{media_digest('1:2', media_identity('9', 1000))}.audio").exists())


if __name__ == "__main__":
    unittest.main()
