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
        result = self.media.clear_cache()
        self.assertGreaterEqual(result["removedBytes"], 1600)
        self.assertEqual([], list(self.media.media_directory.iterdir()))


if __name__ == "__main__":
    unittest.main()
