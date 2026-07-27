import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

from core import Database, RangeNotSatisfiable, now_ts, parse_lrc, parse_range_header, weighted_shuffle_tracks
from telegram_service import LoginFlow, MEDIA_CHUNK_SIZE, TelegramService


class CoreTests(unittest.TestCase):
    def test_ranges(self):
        value = parse_range_header("bytes=0-99", 1000)
        self.assertEqual((0, 99, True), (value.start, value.end, value.partial))
        self.assertEqual((900, 999), (parse_range_header("bytes=-100", 1000).start, parse_range_header("bytes=-100", 1000).end))
        value = parse_range_header(None, 1000)
        self.assertEqual((0, 999, False), (value.start, value.end, value.partial))
        with self.assertRaises(RangeNotSatisfiable):
            parse_range_header("bytes=1000-", 1000)

    def test_lrc_parser_sorts_and_supports_multiple_timestamps(self):
        lines = parse_lrc("[00:02.00][00:01.50]Hello\n[00:03]World", 10_000)
        self.assertEqual([1500, 2000, 3000], [line["startMs"] for line in lines])
        self.assertEqual("Hello", lines[0]["text"])

    def test_local_metadata_survives_telegram_resync(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            track = {"chatId": "1", "messageId": "2", "fileName": "song.mp3", "mimeType": "audio/mpeg", "title": "Telegram title", "artist": "Telegram artist"}
            database.upsert_tracks([track])
            database.save_metadata_patch("1", "2", {"title": "My title"}, [])
            database.upsert_tracks([{**track, "title": "Changed upstream"}])
            self.assertEqual("My title", database.get_track("1", "2")["metadata"]["title"])
            self.assertEqual("Changed upstream", database.get_track("1", "2")["telegramMetadata"]["title"])
            database.close()

    def test_unselect_search_and_unlimited_library(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            tracks = [
                {"chatId": "1", "messageId": str(index), "fileName": f"song-{index}.mp3", "mimeType": "audio/mpeg", "title": f"Telegram title {index}", "artist": "Artist"}
                for index in range(5001)
            ]
            database.upsert_tracks(tracks)
            database.save_metadata_patch("1", "2", {"title": "Needle Remix"}, [])
            keys = []
            for offset in range(0, 5200, 200):
                page = database.list_tracks(offset=offset, limit=200)
                keys.extend(item["key"] for item in page["items"])
                if len(keys) >= page["total"]:
                    break
            self.assertEqual(5001, len(keys))
            self.assertEqual(5001, len(set(keys)))
            self.assertEqual(5001, len(database.playback_queue("1")))
            self.assertEqual(5001, len(database.playback_queue("1", shuffle=True)))
            self.assertEqual("1:2", database.list_tracks(query="Needle")["items"][0]["key"])
            self.assertEqual(4998, database.track_position("1:2"))
            database.set_liked("1:2", True)
            self.assertEqual(["1:2"], [item["key"] for item in database.list_tracks(liked=True)["items"]])
            database.set_source_selected("1", False)
            self.assertEqual([], database.list_tracks()["items"])
            self.assertEqual("1:2", database.list_tracks(query="Needle", include_unselected=True)["items"][0]["key"])
            self.assertEqual("Needle Remix", database.get_track("1", "2")["metadata"]["title"])
            database.close()

    def test_short_queries_use_the_overrides_join(self):
        # One and two character queries fall back to LIKE against o.payload instead of FTS,
        # so every query that builds a WHERE clause needs the overrides join in scope.
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            database.upsert_tracks([
                {"chatId": "1", "messageId": "1", "fileName": "a.mp3", "mimeType": "audio/mpeg", "title": "Zebra"},
                {"chatId": "1", "messageId": "2", "fileName": "b.mp3", "mimeType": "audio/mpeg", "title": "Walrus"},
            ])
            database.save_metadata_patch("1", "2", {"title": "Zeppelin"}, [])
            for query, expected in (("z", 2), ("ze", 2), ("wa", 0), ("zeb", 1)):
                page = database.list_tracks(query=query)
                self.assertEqual(expected, page["total"], f"query={query!r}")
                self.assertEqual(expected, len(page["items"]), f"query={query!r}")
            database.close()

    def test_unselected_source_still_lists_its_own_tracks(self):
        # Clicking the player title to locate a track asks for one chat_id. That is an explicit
        # choice, so the source must list its tracks even while unselected -- it used to come
        # back empty and look like the source had lost everything until a resync.
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Kept"})
            database.upsert_source({"chatId": "2", "kind": "channel", "title": "Dropped"})
            database.upsert_tracks([
                {"chatId": "1", "messageId": "1", "fileName": "a.mp3", "mimeType": "audio/mpeg"},
                {"chatId": "2", "messageId": "1", "fileName": "b.mp3", "mimeType": "audio/mpeg"},
                {"chatId": "2", "messageId": "2", "fileName": "c.mp3", "mimeType": "audio/mpeg"},
            ])
            database.set_source_selected("2", False)

            page = database.list_tracks(chat_id="2")
            self.assertEqual(2, page["total"])
            self.assertEqual(2, len(page["items"]))
            # The combined library still hides it, which is what "unselected" means there.
            self.assertEqual(1, database.list_tracks()["total"])
            self.assertEqual(1, database.list_tracks(chat_id="1")["total"])
            database.close()

    def test_weighted_shuffle_has_no_duplicates_and_tails_recent_tracks(self):
        current = now_ts()
        items = [
            {"key": str(index), "playCount": index, "lastStartedAt": current - index if index < 20 else 0, "lastPlayedAt": 0}
            for index in range(30)
        ]
        values = iter((index + 1) / 31 for index in range(30))
        result = weighted_shuffle_tracks(items, random_value=lambda: next(values))
        self.assertEqual(30, len(result))
        self.assertEqual(30, len(set(result)))
        self.assertEqual({str(index) for index in range(20)}, set(result[-20:]))


class FakeLoginClient:
    def __init__(self):
        self.disconnects = 0
        self.password_attempts = 0

    async def sign_in(self, **_: str) -> None:
        self.password_attempts += 1
        raise RuntimeError("PASSWORD_HASH_INVALID")

    async def disconnect(self) -> None:
        self.disconnects += 1


class FakeSearchClient:
    def __init__(self, messages, dialogs=()):
        self.messages = messages
        self.dialogs = dialogs
        self.searches = 0

    def is_connected(self):
        return True

    async def iter_messages(self, *_, **__):
        self.searches += 1
        for message in self.messages:
            yield message

    async def iter_dialogs(self):
        for dialog in self.dialogs:
            yield dialog


class LoginFlowTests(unittest.IsolatedAsyncioTestCase):
    def service(self, directory: str) -> TelegramService:
        return TelegramService(
            Database(Path(directory) / "library.sqlite3"),
            api_id=1,
            api_hash="test",
            encryption_key=Fernet.generate_key().decode(),
            data_directory=Path(directory),
        )

    async def test_password_failure_stays_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            client = FakeLoginClient()
            service.flows["flow"] = LoginFlow("flow", "phone", client, state="password_required")
            for _ in range(2):
                result = await service.submit_password("flow", "wrong")
                self.assertEqual("password_required", result["state"])
                self.assertEqual("The Telegram 2FA password is incorrect", result["error"])
            self.assertEqual(2, client.password_attempts)
            service.database.close()

    async def test_replacing_login_disconnects_the_old_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            client = FakeLoginClient()
            task = asyncio.create_task(asyncio.sleep(60))
            service.flows["old"] = LoginFlow("old", "qr", client, task=task)
            await service._discard_flows()
            self.assertTrue(task.cancelled())
            self.assertEqual(1, client.disconnects)
            self.assertEqual({}, service.flows)
            service.database.close()

    async def test_global_search_is_live_and_persists_results_for_playback(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            entity = SimpleNamespace(title="Remote music", username="remote")
            message = SimpleNamespace(chat=entity, chat_id=22, id=7, date=None)
            dialog_entity = SimpleNamespace(username="result_channel")
            dialog = SimpleNamespace(id=23, name="Result archive", entity=dialog_entity)
            client = FakeSearchClient([message], [dialog])
            service.client = client
            service.classify_entity = lambda _: "channel"
            service._message_to_track = lambda *_: {
                "chatId": "22", "messageId": "7", "fileName": "result.mp3",
                "mimeType": "audio/mpeg", "title": "Search result", "artist": "Artist",
            }
            first = await asyncio.wait_for(service.global_music_search("result", 10), 2)
            second = await asyncio.wait_for(service.global_music_search("result", 10), 2)
            self.assertEqual(2, client.searches)
            self.assertEqual("22:7", first["tracks"][0]["key"])
            self.assertEqual("22:7", second["tracks"][0]["key"])
            self.assertFalse(service.database.get_source("22")["selected"])
            self.assertEqual("Result archive", service.database.get_source("23")["title"])
            service.database.close()

    async def test_tagged_download_uses_local_metadata_without_touching_original(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            original = Path(directory) / "original.audio"
            original.write_bytes(b"original")
            service.cache_media = AsyncMock(return_value=original)
            track = {
                "key": "1:2", "chatId": "1", "messageId": "2", "documentId": "3",
                "file": {"name": "song.mp3", "size": 8},
                "metadata": {"title": "Edited title", "artist": "Edited artist"},
                "overrides": {"title": "Edited title"},
            }
            commands = []

            async def fake_exec(*command, **_):
                commands.append(command)
                Path(command[-1]).write_bytes(b"tagged")
                return SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))

            with patch("asyncio.create_subprocess_exec", fake_exec):
                result = await service.tagged_download(track)
            self.assertEqual(b"original", original.read_bytes())
            self.assertEqual(b"tagged", result.read_bytes())
            self.assertIn("title=Edited title", commands[0])
            self.assertIn("artist=Edited artist", commands[0])
            service.database.close()

    async def test_media_cache_resumes_partial_and_separates_changed_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            total = MEDIA_CHUNK_SIZE + 17
            track = {
                "key": "1:2", "chatId": "1", "messageId": "2", "documentId": "9",
                "file": {"name": "song.mp3", "size": total},
            }
            service.get_message_document = AsyncMock(return_value=(None, SimpleNamespace(id=9, size=total)))

            async def interrupted(_, start, length):
                self.assertEqual((0, total), (start, length))
                yield b"a" * MEDIA_CHUNK_SIZE
                raise asyncio.CancelledError

            service.iter_media = interrupted
            with self.assertRaises(asyncio.CancelledError):
                await service.cache_media(track)
            partial = next(service.media_directory.glob("*.part"))
            self.assertEqual(MEDIA_CHUNK_SIZE, partial.stat().st_size)

            async def resumed(_, start, length):
                self.assertEqual((MEDIA_CHUNK_SIZE, 17), (start, length))
                yield b"b" * length

            service.iter_media = resumed
            result = await service.cache_media(track)
            self.assertEqual(b"a" * MEDIA_CHUNK_SIZE + b"b" * 17, result.read_bytes())
            self.assertEqual([], list(service.media_directory.glob("*.part")))

            changed = {**track, "documentId": "10"}
            service.get_message_document = AsyncMock(return_value=(None, SimpleNamespace(id=10, size=total)))

            async def fresh(_, start, length):
                self.assertEqual((0, total), (start, length))
                yield b"c" * MEDIA_CHUNK_SIZE
                yield b"d" * (length - MEDIA_CHUNK_SIZE)

            service.iter_media = fresh
            replacement = await service.cache_media(changed)
            self.assertNotEqual(result, replacement)
            self.assertFalse(result.exists())
            abandoned = service.media_directory / "abandoned.part"
            abandoned.write_bytes(b"partial")
            removed = service.clear_media_cache()
            self.assertGreaterEqual(removed["removedBytes"], total + len(b"partial"))
            self.assertEqual([], list(service.media_directory.iterdir()))
            service.database.close()

    async def test_sync_source_runs_per_source_in_parallel_and_caps_total(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            # 4 sources, semaphore(3) means 3 run concurrently and the 4th waits.
            for chat_id in ("1", "2", "3", "4"):
                service.database.upsert_source(
                    {"chatId": chat_id, "kind": "channel", "title": f"S{chat_id}", "selected": True}
                )
            in_flight = 0
            peak = 0
            started = asyncio.Event()
            proceed = asyncio.Event()
            service.client = SimpleNamespace(is_connected=lambda: True)

            async def fake_get_entity(chat_id: int) -> int:
                return chat_id

            async def fake_iter_messages(*_args, **_kwargs):
                nonlocal in_flight, peak
                in_flight += 1
                peak = max(peak, in_flight)
                started.set()
                await proceed.wait()
                in_flight -= 1
                if False:  # pragma: no cover - keep this an async generator
                    yield None

            service.database.get_source = lambda chat_id: {"chatId": chat_id, "selected": True, "lastMessageId": 0}
            service.require_client = lambda: SimpleNamespace(
                is_connected=lambda: True,
                get_entity=AsyncMock(side_effect=fake_get_entity),
                iter_messages=fake_iter_messages,
            )
            tasks = [asyncio.create_task(service.sync_source(chat_id)) for chat_id in ("1", "2", "3", "4")]
            # wait until 3 have entered iter_messages; the 4th must be parked on the semaphore.
            await asyncio.wait_for(started.wait(), 2)
            for _ in range(50):
                if in_flight >= 3:
                    break
                await asyncio.sleep(0)
            self.assertEqual(3, in_flight, "semaphore should cap concurrent syncs at 3")
            proceed.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks), 2)
            self.assertEqual(4, len(results))
            self.assertLessEqual(peak, 3)
            self.assertIn("1", service.sync_locks)
            service.database.close()

    async def test_sync_source_serializes_same_chat_id_with_per_source_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            service.database.upsert_source(
                {"chatId": "1", "kind": "channel", "title": "S1", "selected": True}
            )
            in_flight = 0
            peak = 0
            started = asyncio.Event()
            proceed = asyncio.Event()
            service.client = SimpleNamespace(is_connected=lambda: True)
            service.database.get_source = lambda _chat_id: {"chatId": "1", "selected": True, "lastMessageId": 0}

            async def fake_get_entity(_chat_id: int) -> int:
                return 1

            async def fake_iter_messages(*_args, **_kwargs):
                nonlocal in_flight, peak
                in_flight += 1
                peak = max(peak, in_flight)
                started.set()
                await proceed.wait()
                in_flight -= 1
                if False:  # pragma: no cover
                    yield None

            service.require_client = lambda: SimpleNamespace(
                is_connected=lambda: True,
                get_entity=AsyncMock(side_effect=fake_get_entity),
                iter_messages=fake_iter_messages,
            )
            t1 = asyncio.create_task(service.sync_source("1"))
            t2 = asyncio.create_task(service.sync_source("1"))
            await asyncio.wait_for(started.wait(), 2)
            proceed.set()
            await asyncio.wait_for(asyncio.gather(t1, t2), 2)
            self.assertLessEqual(peak, 1, "per-source lock must serialize the same chat_id")
            service.database.close()


if __name__ == "__main__":
    unittest.main()
