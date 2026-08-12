import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
from telethon.errors import RPCError
from telethon.tl import functions
from telethon.tl.types import User
from telethon.tl.types import contacts as contacts_types

from core import Database, RangeNotSatisfiable, media_digest, media_identity, now_ts, parse_lrc, parse_range_header, weighted_shuffle_tracks
from media import MEDIA_CHUNK_SIZE
from telegram_service import QR_QUIET_MODULES, LoginFlow, TelegramService, render_qr_svg


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

    def test_qr_svg_scales_and_keeps_a_quiet_zone(self):
        import re

        import segno

        payload = "tg://login?token=AbCdEf0123456789"
        svg = render_qr_svg(payload)
        modules = len(segno.make(payload).matrix)
        extent = modules + QR_QUIET_MODULES * 2
        # A viewBox with no width/height is what lets the stylesheet size the code; segno's own
        # writer emits fixed pixels instead, which is why this is rendered by hand.
        self.assertIn(f'viewBox="0 0 {extent} {extent}"', svg)
        self.assertNotRegex(svg, r"<svg[^>]*\swidth=")
        # The quiet zone must be inside the artwork, so it cannot be lost by the surrounding CSS.
        self.assertGreaterEqual(QR_QUIET_MODULES, 2, "below this the code stops decoding")
        # Every drawn module must sit within the quiet margin on all four sides.
        coordinates = [float(value) for value in re.findall(r"[MHV](-?\d+(?:\.\d+)?)", svg)]
        drawn = [value for value in coordinates if value != 0.0]
        self.assertGreater(min(drawn), 0, "a module touches the edge, leaving no quiet zone")
        self.assertLessEqual(max(drawn), extent - QR_QUIET_MODULES)

    def test_database_files_are_not_world_readable(self):
        # The database holds the Fernet-encrypted Telegram session and every chat title, so it
        # must not be left at the umask default. WAL sidecars carry the same pages.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "library.sqlite3"
            database = Database(path)
            database.list_sources(False)
            try:
                self.assertEqual(0o700, path.parent.stat().st_mode & 0o777)
                for target in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
                    self.assertTrue(target.exists(), f"{target.name} missing")
                    self.assertEqual(0, target.stat().st_mode & 0o077, f"{target.name} is group/world readable")
            finally:
                database.close()

    def test_reopening_a_loose_database_tightens_it(self):
        # Existing installs were created at 0644, so opening must repair them rather than only
        # getting new databases right.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "library.sqlite3"
            Database(path).close()
            path.chmod(0o644)
            path.parent.chmod(0o755)
            Database(path).close()
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertEqual(0o700, path.parent.stat().st_mode & 0o777)

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

    def test_library_rows_report_liked_the_same_as_queue_rows(self):
        # list_tracks omitted t.liked_at from its SELECT, so _track_summary's
        # value.get("liked_at") was always None and every library row rendered un-liked --
        # while the queue, which goes through track_summaries, showed the heart correctly.
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            database.upsert_tracks([{"chatId": "1", "messageId": "2", "fileName": "song.mp3",
                                     "mimeType": "audio/mpeg", "title": "T", "artist": "A"}])
            database.set_liked("1:2", True)

            self.assertIs(True, database.list_tracks()["items"][0]["liked"])
            # Both read paths, asserted together, so they cannot diverge again.
            self.assertIs(True, database.track_summaries(["1:2"])[0]["liked"])
            # And unliking has to come back through the same path.
            database.set_liked("1:2", False)
            self.assertIs(False, database.list_tracks()["items"][0]["liked"])
            self.assertIs(False, database.track_summaries(["1:2"])[0]["liked"])
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

    def test_track_sort_uses_an_allowlist_and_matches_what_is_displayed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            database.upsert_tracks([
                {"chatId": "1", "messageId": "1", "fileName": "z.mp3", "mimeType": "audio/mpeg",
                 "title": "Zebra", "artist": "Beta", "durationMs": 300_000, "sentAt": 300},
                {"chatId": "1", "messageId": "2", "fileName": "a.mp3", "mimeType": "audio/mpeg",
                 "title": "apple", "artist": "Alpha", "durationMs": 100_000, "sentAt": 200},
                {"chatId": "1", "messageId": "3", "fileName": "m.mp3", "mimeType": "audio/mpeg",
                 "title": "Mango", "artist": "Gamma", "durationMs": 200_000, "sentAt": 100},
            ])
            # The displayed title wins over the Telegram one, so sorting must follow the override.
            database.save_metadata_patch("1", "1", {"title": "Aardvark override"}, [])

            def keys(sort):
                return [item["key"] for item in database.list_tracks(sort=sort)["items"]]

            self.assertEqual(["1:1", "1:2", "1:3"], keys("posted"))
            # Aardvark override first, then apple -- COLLATE NOCASE, or "apple" would follow "Mango".
            self.assertEqual(["1:1", "1:2", "1:3"], keys("title"))
            self.assertEqual(["1:2", "1:1", "1:3"], keys("artist"))
            self.assertEqual(["1:1", "1:3", "1:2"], keys("duration"))

            # Anything not on the allowlist degrades to posted rather than reaching SQL.
            for hostile in ["title; DROP TABLE tracks", "t.sent_at ASC", "", "nonsense", None]:
                self.assertEqual(keys("posted"), keys(hostile), f"{hostile!r} was not rejected")
            # And the table is still there.
            self.assertEqual(3, database.list_tracks()["total"])
            database.close()

    def test_track_pages_report_authoritative_all_music_total_and_utc_day_breaks(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Selected"})
            database.upsert_source({"chatId": "2", "kind": "channel", "title": "Hidden"})
            database.upsert_tracks([
                {"chatId": "1", "messageId": "1", "fileName": "a.mp3", "mimeType": "audio/mpeg", "title": "Needle", "sentAt": 1753835400},
                {"chatId": "1", "messageId": "2", "fileName": "b.mp3", "mimeType": "audio/mpeg", "title": "Other", "sentAt": 1753749000},
                {"chatId": "1", "messageId": "3", "fileName": "c.mp3", "mimeType": "audio/mpeg", "title": "Unknown date", "sentAt": 0},
                {"chatId": "2", "messageId": "1", "fileName": "d.mp3", "mimeType": "audio/mpeg", "title": "Hidden", "sentAt": 1753835400},
            ])
            database.set_source_selected("2", False)

            page = database.list_tracks(query="Needle")
            self.assertEqual(1, page["total"], "active-view total remains query-specific")
            self.assertEqual(3, page["allMusicTotal"], "selected, available tracks are authoritative")
            self.assertEqual([{"index": 0, "dayKey": "2025-07-30"}], page["dayBreaks"])

            full = database.list_tracks()
            self.assertEqual([
                {"index": 0, "dayKey": "2025-07-30"},
                {"index": 1, "dayKey": "2025-07-29"},
            ], full["dayBreaks"], "invalid/non-positive timestamps do not create rules")
            database.mark_unavailable("1", ["2"])
            self.assertEqual(2, database.list_tracks()["allMusicTotal"])

            for kwargs in ({"chat_id": "1"}, {"liked": True}, {"sort": "title"}):
                self.assertEqual([], database.list_tracks(**kwargs)["dayBreaks"])
            database.close()

    def test_mark_unavailable_refreshes_cached_source_track_counts(self):
        # mark_unavailable used to skip the _track_counts invalidation, so a deleted Telegram
        # message left list_sources reporting a stale count until the next full sync.
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            database.upsert_tracks([
                {"chatId": "1", "messageId": str(index), "fileName": f"song-{index}.mp3",
                 "mimeType": "audio/mpeg", "title": f"Track {index}", "artist": "Artist"}
                for index in range(5)
            ])
            self.assertEqual(5, database.list_sources()[0]["trackCount"])
            database.mark_unavailable("1", ["2"])
            self.assertEqual(4, database.list_sources()[0]["trackCount"])
            # An update that changes no rows must not waste a cache rebuild.
            database.mark_unavailable("1", ["999"])
            self.assertEqual(4, database.list_sources()[0]["trackCount"])
            database.close()

    def test_track_position_matches_each_allowlisted_track_sort(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            database.upsert_tracks([
                {"chatId": "1", "messageId": "1", "fileName": "z.mp3", "mimeType": "audio/mpeg",
                 "title": "Zebra", "artist": "Beta", "durationMs": 300_000, "sentAt": 300},
                {"chatId": "1", "messageId": "2", "fileName": "a.mp3", "mimeType": "audio/mpeg",
                 "title": "apple", "artist": "Alpha", "durationMs": 100_000, "sentAt": 200},
                {"chatId": "1", "messageId": "3", "fileName": "m.mp3", "mimeType": "audio/mpeg",
                 "title": "Mango", "artist": "Gamma", "durationMs": 200_000, "sentAt": 100},
            ])
            database.save_metadata_patch("1", "1", {"title": "Aardvark override"}, [])

            self.assertEqual(0, database.track_position("1:1", chat_id="1", sort="posted"))
            self.assertEqual(1, database.track_position("1:2", chat_id="1", sort="title"))
            self.assertEqual(1, database.track_position("1:1", chat_id="1", sort="artist"))
            self.assertEqual(2, database.track_position("1:2", chat_id="1", sort="duration"))
            # Position uses the same allowlist fallback as list_tracks, never client SQL.
            self.assertEqual(
                database.track_position("1:1", chat_id="1", sort="posted"),
                database.track_position("1:1", chat_id="1", sort="t.sent_at ASC"),
            )
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


class FakeCancellingSyncClient:
    """Yields a few messages, then behaves as if the caller cancelled the scan."""

    def __init__(self, messages, cancel_after):
        self.messages = messages
        self.cancel_after = cancel_after

    def is_connected(self):
        return True

    async def get_entity(self, *_, **__):
        return SimpleNamespace(title="Preview channel")

    async def iter_messages(self, *_, **__):
        for index, message in enumerate(self.messages):
            if index == self.cancel_after:
                raise asyncio.CancelledError()
            yield message


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

    async def test_cancelled_preview_saves_tracks_without_advancing_the_cursor(self):
        # Browsing away cancels the preview job. Keep whatever was already read, but leave
        # lastMessageId alone: iter_messages walks newest to oldest, so advancing it after a
        # partial scan would make the next incremental sync skip every older message.
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            service.database.upsert_source({"chatId": "55", "kind": "channel", "title": "Preview"})
            messages = [SimpleNamespace(id=index) for index in (40, 30, 20, 10)]
            service.client = FakeCancellingSyncClient(messages, cancel_after=2)
            service._message_to_track = lambda message, chat_id: {
                "chatId": chat_id, "messageId": str(message.id),
                "fileName": f"{message.id}.mp3", "mimeType": "audio/mpeg",
            }
            with self.assertRaises(asyncio.CancelledError):
                await service.sync_source("55", full=True, temporary=True)
            source = service.database.get_source("55")
            self.assertEqual(0, int(source["lastMessageId"] or 0), "cursor must not advance")
            self.assertEqual(2, service.database.list_tracks(chat_id="55")["total"])
            service.database.close()

    async def test_completed_sync_advances_the_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            service.database.upsert_source({"chatId": "56", "kind": "channel", "title": "Done"})
            messages = [SimpleNamespace(id=index) for index in (40, 30, 20, 10)]
            service.client = FakeCancellingSyncClient(messages, cancel_after=None)
            service._message_to_track = lambda message, chat_id: {
                "chatId": chat_id, "messageId": str(message.id),
                "fileName": f"{message.id}.mp3", "mimeType": "audio/mpeg",
            }
            await service.sync_source("56", full=True, temporary=True)
            self.assertEqual(40, int(service.database.get_source("56")["lastMessageId"] or 0))
            self.assertEqual(4, service.database.list_tracks(chat_id="56")["total"])
            service.database.close()

    async def test_completed_full_sync_marks_unseen_tracks_unavailable(self):
        # A finished full scan flips tracks whose messages vanished from Telegram: the
        # source holds 40/30/20/10/5, the scan only re-sees 40/30/20/10, so 5 must drop out.
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            service.database.upsert_source({"chatId": "57", "kind": "channel", "title": "Done"})
            service.database.upsert_tracks([
                {"chatId": "57", "messageId": str(message_id), "fileName": f"{message_id}.mp3",
                 "mimeType": "audio/mpeg", "title": f"Track {message_id}", "artist": "Artist"}
                for message_id in (40, 30, 20, 10, 5)
            ])
            service.client = FakeCancellingSyncClient(
                [SimpleNamespace(id=index) for index in (40, 30, 20, 10)], cancel_after=None
            )
            service._message_to_track = lambda message, chat_id: {
                "chatId": chat_id, "messageId": str(message.id),
                "fileName": f"{message.id}.mp3", "mimeType": "audio/mpeg",
            }
            await service.sync_source("57", full=True)
            self.assertEqual(4, service.database.list_tracks(chat_id="57")["total"])
            self.assertFalse(service.database.get_track("57", "5")["available"])
            service.database.close()

    async def test_interrupted_full_sync_keeps_unseen_tracks_available(self):
        # The scan dies mid-way (cancel_after=1, after message 40). It never completed, so
        # none of the tracks it did not re-see may be marked unavailable.
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            service.database.upsert_source({"chatId": "58", "kind": "channel", "title": "Interrupted"})
            service.database.upsert_tracks([
                {"chatId": "58", "messageId": str(message_id), "fileName": f"{message_id}.mp3",
                 "mimeType": "audio/mpeg", "title": f"Track {message_id}", "artist": "Artist"}
                for message_id in (40, 30, 20, 10)
            ])
            service.client = FakeCancellingSyncClient(
                [SimpleNamespace(id=index) for index in (40, 30)], cancel_after=1
            )
            service._message_to_track = lambda message, chat_id: {
                "chatId": chat_id, "messageId": str(message.id),
                "fileName": f"{message.id}.mp3", "mimeType": "audio/mpeg",
            }
            with self.assertRaises(asyncio.CancelledError):
                await service.sync_source("58", full=True)
            self.assertEqual(4, service.database.list_tracks(chat_id="58")["total"])
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
            service.media.cache_media = AsyncMock(return_value=original)
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
                result = await service.media.tagged_download(track)
            self.assertEqual(b"original", original.read_bytes())
            self.assertEqual(b"tagged", result.read_bytes())
            self.assertIn("title=Edited title", commands[0])
            self.assertIn("artist=Edited artist", commands[0])
            service.database.close()

    async def test_tagged_download_reports_when_ffmpeg_is_missing(self):
        # Docker images built from python:*-slim carry no ffmpeg, so this is the container default,
        # not a rare edge case. The caller falls back to the untagged original either way; the
        # point is that the reason is no longer swallowed.
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            original = Path(directory) / "original.audio"
            original.write_bytes(b"original")
            service.media.cache_media = AsyncMock(return_value=original)
            track = {
                "key": "1:2", "chatId": "1", "messageId": "2", "documentId": "3",
                "file": {"name": "song.mp3", "size": 8},
                "metadata": {"title": "Edited title"},
                "overrides": {"title": "Edited title"},
            }

            async def missing_ffmpeg(*_command, **_kwargs):
                raise FileNotFoundError(2, "No such file or directory", "ffmpeg")

            with patch("asyncio.create_subprocess_exec", missing_ffmpeg):
                with self.assertLogs("media", level="WARNING") as logs:
                    result = await service.media.tagged_download(track)
            self.assertIsNone(result)
            self.assertEqual(b"original", original.read_bytes())
            output = "\n".join(logs.output)
            self.assertIn("ffmpeg", output)
            self.assertIn("1:2", output)
            service.database.close()

    async def test_tagged_download_reports_ffmpeg_failure_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            original = Path(directory) / "original.audio"
            original.write_bytes(b"original")
            service.media.cache_media = AsyncMock(return_value=original)
            track = {
                "key": "1:2", "chatId": "1", "messageId": "2", "documentId": "3",
                "file": {"name": "song.mp3", "size": 8},
                "metadata": {"title": "Edited title"},
                "overrides": {"title": "Edited title"},
            }
            partials = []

            async def failing_exec(*command, **_kwargs):
                # ffmpeg can leave a half-written file behind when it fails partway.
                Path(command[-1]).write_bytes(b"partial")
                partials.append(Path(command[-1]))
                return SimpleNamespace(
                    returncode=1,
                    communicate=AsyncMock(return_value=(b"", b"Invalid data found")),
                )

            with patch("asyncio.create_subprocess_exec", failing_exec):
                with self.assertLogs("media", level="WARNING") as logs:
                    result = await service.media.tagged_download(track)
            self.assertIsNone(result)
            self.assertEqual(b"original", original.read_bytes())
            # The partial output must not be left lying around to be served later.
            self.assertFalse(partials[0].exists())
            # ffmpeg's own diagnosis is the useful part; it used to be captured and dropped.
            self.assertIn("Invalid data found", "\n".join(logs.output))
            service.database.close()

    async def test_media_cache_resumes_partial_and_separates_changed_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            media = service.media
            total = MEDIA_CHUNK_SIZE + 17
            track = {
                "key": "1:2", "chatId": "1", "messageId": "2", "documentId": "9",
                "file": {"name": "song.mp3", "size": total},
            }
            media.get_message_document = AsyncMock(return_value=(None, SimpleNamespace(id=9, size=total)))

            async def interrupted(_, start, length):
                self.assertEqual((0, total), (start, length))
                yield b"a" * MEDIA_CHUNK_SIZE
                raise asyncio.CancelledError

            media.iter_media = interrupted
            with self.assertRaises(asyncio.CancelledError):
                await media.cache_media(track)
            partial = next(media.media_directory.glob("*.part"))
            self.assertEqual(MEDIA_CHUNK_SIZE, partial.stat().st_size)

            async def resumed(_, start, length):
                self.assertEqual((MEDIA_CHUNK_SIZE, 17), (start, length))
                yield b"b" * length

            media.iter_media = resumed
            result = await media.cache_media(track)
            self.assertEqual(b"a" * MEDIA_CHUNK_SIZE + b"b" * 17, result.read_bytes())
            self.assertEqual([], list(media.media_directory.glob("*.part")))

            changed = {**track, "documentId": "10"}
            media.get_message_document = AsyncMock(return_value=(None, SimpleNamespace(id=10, size=total)))

            async def fresh(_, start, length):
                self.assertEqual((0, total), (start, length))
                yield b"c" * MEDIA_CHUNK_SIZE
                yield b"d" * (length - MEDIA_CHUNK_SIZE)

            media.iter_media = fresh
            replacement = await media.cache_media(changed)
            self.assertNotEqual(result, replacement)
            self.assertFalse(result.exists())
            abandoned = media.media_directory / "abandoned.part"
            abandoned.write_bytes(b"partial")
            removed = await media.clear_all()
            self.assertGreaterEqual(removed["removedBytes"], total + len(b"partial"))
            self.assertEqual([], list(media.media_directory.iterdir()))
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


class ContactRankingTests(unittest.IsolatedAsyncioTestCase):
    """contacts() annotates saved contacts with Telegram's frequent-forward rating."""

    def service(self, directory: str) -> TelegramService:
        return TelegramService(
            Database(Path(directory) / "library.sqlite3"),
            api_id=1,
            api_hash="test",
            encryption_key=Fernet.generate_key().decode(),
            data_directory=Path(directory),
        )

    @staticmethod
    def _users():
        # Real User instances: utils.get_display_name() type-checks its argument, so a
        # SimpleNamespace silently degrades every name to "Unnamed contact".
        return [
            User(id=1, first_name="Ada", username="ada", bot=False, deleted=False, is_self=False),
            User(id=2, first_name="Bo", bot=False, deleted=False, is_self=False),
            User(id=3, first_name="Cy", bot=False, deleted=False, is_self=False),
        ]

    def _client(self, top_peers_result):
        async def call(request):
            if isinstance(request, functions.contacts.GetTopPeersRequest):
                if isinstance(top_peers_result, Exception):
                    raise top_peers_result
                return top_peers_result
            return SimpleNamespace(users=self._users())

        return call

    async def test_ranks_only_saved_contacts_and_keeps_alphabetical_order(self):
        # Top peers include a bot (99) and a non-contact (42). Neither may appear: forward_track()
        # validates the recipient against this same list, so ranking must not widen it.
        peers = SimpleNamespace(peers=[
            SimpleNamespace(peer=SimpleNamespace(user_id=3), rating=9.0),
            SimpleNamespace(peer=SimpleNamespace(user_id=99), rating=8.0),
            SimpleNamespace(peer=SimpleNamespace(user_id=1), rating=2.0),
            SimpleNamespace(peer=SimpleNamespace(user_id=42), rating=1.0),
        ])
        result = contacts_types.TopPeers(
            categories=[SimpleNamespace(category=None, peers=peers.peers)], chats=[], users=[])
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            service.require_client = lambda: self._client(result)
            people = await service.contacts()
            self.assertEqual(["Ada", "Bo", "Cy"], [item["name"] for item in people])
            self.assertEqual({"Ada": 2.0, "Bo": None, "Cy": 9.0},
                             {item["name"]: item["forwardRank"] for item in people})
            service.database.close()

    async def test_disabled_or_failing_top_peers_still_returns_contacts(self):
        # Frequent contacts are a nicety. A user who turned off suggestions, or a flood wait,
        # must not take down the whole share picker.
        for outcome in (contacts_types.TopPeersDisabled(), RPCError("req", "FLOOD_WAIT_5", 420)):
            with tempfile.TemporaryDirectory() as directory:
                service = self.service(directory)
                service.require_client = lambda: self._client(outcome)
                people = await service.contacts()
                self.assertEqual(["Ada", "Bo", "Cy"], [item["name"] for item in people])
                self.assertTrue(all(item["forwardRank"] is None for item in people),
                                f"{type(outcome).__name__} must degrade to an unranked list")
                service.database.close()


class QueueWindowTests(unittest.TestCase):
    """playback_queue can return a slice around the current track instead of every key."""

    # Tracks have no sentAt, so the library orders by rowid DESC: full[k] is "1:{count-1-k}".
    def _full(self, database, count: int) -> list[str]:
        return [f"1:{count - 1 - index}" for index in range(count)]

    def test_no_window_returns_the_full_list_as_before(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            database.upsert_tracks([
                {"chatId": "1", "messageId": str(index), "fileName": f"song-{index}.mp3",
                 "mimeType": "audio/mpeg", "title": f"Track {index}", "artist": "Artist"}
                for index in range(5001)
            ])
            try:
                self.assertEqual(5001, len(database.playback_queue("1")))
            finally:
                database.close()

    def test_window_slices_around_the_current_track(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            database.upsert_tracks([
                {"chatId": "1", "messageId": str(index), "fileName": f"song-{index}.mp3",
                 "mimeType": "audio/mpeg", "title": f"Track {index}", "artist": "Artist"}
                for index in range(5001)
            ])
            try:
                full = self._full(database, 5001)
                current = "1:2500"
                result = database.playback_queue(current_key=current, window_before=50, window_after=300)
                self.assertIsInstance(result, dict)
                self.assertEqual(5001, result["total"])
                self.assertEqual(2450, result["offset"])
                # The window is a contiguous slice of the full ordering, current track inside.
                self.assertEqual(full[result["offset"]:result["offset"] + len(result["keys"])], result["keys"])
                self.assertEqual(current, result["keys"][50])
                self.assertEqual(351, len(result["keys"]))
                # Slices must never reach past the ends of the library.
                first = database.playback_queue(current_key="1:4999", window_before=50, window_after=300)
                self.assertEqual(0, first["offset"])
                self.assertEqual("1:5000", first["keys"][0])
                self.assertIn("1:4999", first["keys"][:2])
                last = database.playback_queue(current_key="1:0", window_before=50, window_after=300)
                self.assertLessEqual(last["offset"] + len(last["keys"]), 5001)
            finally:
                database.close()

    def test_window_respects_shuffle_geometry(self):
        # weighted_shuffle_tracks excludes the current track from the result, so a shuffled
        # window has nothing to centre on and starts at the top of the fresh order. The
        # frontend prepends the current track itself when it wants it queued.
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            database.upsert_tracks([
                {"chatId": "1", "messageId": str(index), "fileName": f"song-{index}.mp3",
                 "mimeType": "audio/mpeg", "title": f"Track {index}", "artist": "Artist"}
                for index in range(50)
            ])
            try:
                full = database.playback_queue("1", shuffle=True, current_key="1:25")
                result = database.playback_queue(
                    "1", shuffle=True, current_key="1:25", window_before=5, window_after=5
                )
                self.assertEqual(len(full), result["total"])
                self.assertEqual(6, len(result["keys"]))
                self.assertEqual(0, result["offset"])
                self.assertNotIn("1:25", result["keys"])
                self.assertEqual(len(result["keys"]), len(set(result["keys"])))
                self.assertLessEqual(result["offset"] + len(result["keys"]), 50)
            finally:
                database.close()


class SyncGenerationTests(unittest.TestCase):
    """Full-scan availability via the seen_generation marker, not a NOT IN clause."""

    def test_sync_statements_stay_fixed_shape_beyond_100k_messages(self):
        # A 100k+ message source used to build one "message_id NOT IN (?, ?, ...)" clause
        # per seen id: huge SQL text approaching SQLite's bind limit on large channels.
        # The generation marker keeps every sync statement a fixed small shape.
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            statements = []

            def trace(sql):
                if sql.lstrip().upper().startswith(("UPDATE", "INSERT")):
                    statements.append(sql)

            database.connection.set_trace_callback(trace)
            try:
                generation = database.begin_sync_generation("1")
                self.assertEqual(1, generation)
                for start in range(0, 100_010, 1000):
                    database.upsert_tracks([
                        {"chatId": "1", "messageId": str(index), "fileName": f"song-{index}.mp3",
                         "mimeType": "audio/mpeg", "title": f"Track {index}", "artist": "Artist"}
                        for index in range(start, min(start + 1000, 100_010))
                    ], seen_generation=generation)
                database.complete_sync_generation("1", generation)
            finally:
                database.connection.set_trace_callback(None)
            self.assertEqual(100_010, database.list_sources()[0]["trackCount"])
            self.assertFalse(
                any("NOT IN" in sql.upper() for sql in statements),
                "the sync must not build a placeholder-per-track NOT IN query",
            )
            self.assertLessEqual(
                max(len(sql) for sql in statements), 2048,
                "every sync statement must stay a fixed small shape",
            )

            # A second full scan that never re-sees ten tracks marks exactly those
            # unavailable -- the >100k path completes successfully.
            generation = database.begin_sync_generation("1")
            self.assertEqual(2, generation, "each full scan bumps the generation")
            for start in range(0, 100_000, 1000):
                database.upsert_tracks([
                    {"chatId": "1", "messageId": str(index), "fileName": f"song-{index}.mp3",
                     "mimeType": "audio/mpeg", "title": f"Track {index}", "artist": "Artist"}
                    for index in range(start, min(start + 1000, 100_000))
                ], seen_generation=generation)
            database.complete_sync_generation("1", generation)
            self.assertEqual(100_000, database.list_sources()[0]["trackCount"])
            database.close()

    def test_completed_full_sync_marks_unseen_tracks_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            first = database.begin_sync_generation("1")
            database.upsert_tracks([
                {"chatId": "1", "messageId": str(index), "fileName": f"song-{index}.mp3",
                 "mimeType": "audio/mpeg", "title": f"Track {index}", "artist": "Artist"}
                for index in range(3)
            ], seen_generation=first)
            database.complete_sync_generation("1", first)
            self.assertEqual(3, database.list_tracks(chat_id="1")["total"])

            second = database.begin_sync_generation("1")
            self.assertEqual(2, second, "each full scan bumps the generation")
            database.upsert_tracks([
                {"chatId": "1", "messageId": "1", "fileName": "song-1.mp3",
                 "mimeType": "audio/mpeg", "title": "Track 1", "artist": "Artist"},
                {"chatId": "1", "messageId": "2", "fileName": "song-2.mp3",
                 "mimeType": "audio/mpeg", "title": "Track 2", "artist": "Artist"},
            ], seen_generation=second)
            database.complete_sync_generation("1", second)
            page = database.list_tracks(chat_id="1")
            self.assertEqual(2, page["total"])
            self.assertEqual({"1:1", "1:2"}, {item["key"] for item in page["items"]})
            database.close()

    def test_interrupted_full_sync_keeps_unseen_tracks_available(self):
        # begin_sync_generation + upserts, then no complete_sync_generation: the scan died
        # mid-way, so tracks it never re-seen must stay available.
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            first = database.begin_sync_generation("1")
            database.upsert_tracks([
                {"chatId": "1", "messageId": str(index), "fileName": f"song-{index}.mp3",
                 "mimeType": "audio/mpeg", "title": f"Track {index}", "artist": "Artist"}
                for index in range(3)
            ], seen_generation=first)
            database.complete_sync_generation("1", first)
            self.assertEqual(3, database.list_tracks(chat_id="1")["total"])

            generation = database.begin_sync_generation("1")
            database.upsert_tracks([
                {"chatId": "1", "messageId": "1", "fileName": "song-1.mp3",
                 "mimeType": "audio/mpeg", "title": "Track 1", "artist": "Artist"},
            ], seen_generation=generation)
            # No complete_sync_generation: the interrupted scan must not mark anything.
            self.assertEqual(3, database.list_tracks(chat_id="1")["total"])
            database.close()


class SearchReconcileTests(unittest.TestCase):
    def test_reconcile_rebuilds_a_drifted_fts_index(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            database.upsert_tracks([
                {"chatId": "1", "messageId": str(index), "fileName": f"song-{index}.mp3",
                 "mimeType": "audio/mpeg", "title": f"Track {index}", "artist": "Artist"}
                for index in range(40)
            ])
            # Simulate drift the dirty queue cannot explain: FTS rows missing with no
            # pending search_dirty work left to restore them (orphaned rows, or rows lost
            # before the durable table existed). Any search between upsert and flush would
            # have re-inserted them, so drop the queue to mimic that state.
            with database.transaction() as connection:
                connection.execute("DELETE FROM search_dirty")
                # rowid-based FTS v7 has no key column
                connection.execute(
                    "DELETE FROM tracks_fts WHERE rowid IN (SELECT rowid FROM tracks WHERE chat_id='1' AND message_id LIKE '3_')"
                )
            self.assertEqual(0, database.list_tracks(query="Track 30")["total"])
            # Returns the drift it found (40 tracks missing from the index) after rebuilding.
            self.assertEqual(40, database.reconcile_search())
            self.assertEqual(1, database.list_tracks(query="Track 30")["total"])
            database.close()

    def test_reconcile_leaves_an_up_to_date_index_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            database.upsert_tracks([
                {"chatId": "1", "messageId": str(index), "fileName": f"song-{index}.mp3",
                 "mimeType": "audio/mpeg", "title": f"Track {index}", "artist": "Artist"}
                for index in range(10)
            ])
            # The dirty queue is the index's working memory: flush it, then the counts agree and
            # reconcile must find nothing to do.
            database._flush_search()
            self.assertEqual(0, database.reconcile_search())
            database.close()

    def test_pending_search_updates_survive_a_process_restart(self):
        # A rename is queued in search_dirty in the same transaction as the override, so a
        # crash before the delayed flush cannot lose it. Reopening the database and searching
        # picks the pending work up; the replaced old title must no longer match.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "library.sqlite3"
            database = Database(path)
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            database.upsert_tracks([
                {"chatId": "1", "messageId": "2", "fileName": "song-2.mp3",
                 "mimeType": "audio/mpeg", "title": "Old title", "artist": "Artist"}
            ])
            database.save_metadata_patch("1", "2", {"title": "New title"}, [])
            with database.lock:
                pending = database.connection.execute(
                    "SELECT COUNT(*) FROM search_dirty"
                ).fetchone()[0]
            self.assertEqual(1, pending, "the rename must be queued for the FTS flush")
            # Crash before the flush: closing the raw handle skips Database.close(), which
            # would have flushed the queue. Only the durable rows survive.
            database.connection.close()

            restarted = Database(path)
            try:
                page = restarted.list_tracks(query="New title")
                self.assertEqual(1, page["total"])
                self.assertEqual("1:2", page["items"][0]["key"])
                self.assertEqual(0, restarted.list_tracks(query="Old title")["total"])
            finally:
                restarted.close()

    def test_failed_flush_leaves_dirty_work_pending(self):
        # If the FTS update raises inside the flush transaction, the rollback must keep the
        # search_dirty rows so the next flush retries them.
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            database.upsert_tracks([
                {"chatId": "1", "messageId": "2", "fileName": "song-2.mp3",
                 "mimeType": "audio/mpeg", "title": "Old title", "artist": "Artist"}
            ])
            database.save_metadata_patch("1", "2", {"title": "New title"}, [])
            with patch.object(database, "_update_search", side_effect=sqlite3.OperationalError("boom")):
                with self.assertRaises(sqlite3.OperationalError):
                    database._flush_search()
            with database.lock:
                row = database.connection.execute(
                    "SELECT track_key FROM search_dirty"
                ).fetchone()
            self.assertEqual("1:2", row["track_key"], "a failed flush must not lose the pending key")
            # The retry succeeds and the queue drains.
            database._flush_search()
            self.assertEqual(1, database.list_tracks(query="New title")["total"])
            database.close()


class PrefetchProtectionTests(unittest.IsolatedAsyncioTestCase):
    """start_prefetch must mutate the shared key set, not rebind it (regression: A1)."""

    def service(self, directory: str) -> TelegramService:
        return TelegramService(
            Database(Path(directory) / "library.sqlite3"),
            api_id=1,
            api_hash="test",
            encryption_key=Fernet.generate_key().decode(),
            data_directory=Path(directory),
        )

    async def test_start_prefetch_mutates_the_shared_set_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            service.database.save_settings({"prefetchCount": 5})
            shared = service.prefetch_keys
            self.assertIs(service.media.protected_keys, shared)
            service.start_prefetch(["1:2", "1:3"])
            self.assertIs(service.prefetch_keys, shared, "selection must never rebind the set")
            self.assertIs(service.media.protected_keys, shared)
            self.assertEqual({"1:2", "1:3"}, shared)
            # Let the first job finish (the replacement path cancels a running prefetch,
            # which is JobRunner behavior, not what this regression covers).
            await asyncio.sleep(0)
            # A replacement selection drops the old protection and adopts the new keys.
            service.start_prefetch(["1:4", "1:5"])
            self.assertIs(service.media.protected_keys, shared)
            self.assertEqual({"1:4", "1:5"}, shared)
            self.assertNotIn("1:2", shared)
            service.database.close()

    async def test_eviction_keeps_the_active_prefetch_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            service.database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            cached = []
            for message_id, size in (("2", 1000), ("3", 100), ("4", 100)):
                service.database.upsert_tracks([{
                    "chatId": "1", "messageId": message_id, "fileName": f"song-{message_id}.mp3",
                    "mimeType": "audio/mpeg", "fileSize": size, "title": f"T{message_id}",
                    "artist": "A", "documentId": message_id,
                }])
                identity = media_identity(message_id, size)
                digest = media_digest(f"1:{message_id}", identity)
                path = service.media.media_directory / f"{digest}.audio"
                path.write_bytes(b"x" * size)
                service.database.save_media_cache(f"1:{message_id}", identity, path.name, size)
                cached.append(path)
            service.database.save_settings({"prefetchCount": 2})
            service.start_prefetch(["1:2", "1:3", "1:4"])
            self.assertIs(service.media.protected_keys, service.prefetch_keys)
            # Budget fits the protected pair (1100) but not the third entry (1200), so only
            # the unprotected one may go.
            service.media._evict_cache_sync(maximum=1100)
            self.assertTrue(cached[0].exists(), "prefetched entry must survive eviction")
            self.assertTrue(cached[1].exists(), "prefetched entry must survive eviction")
            self.assertFalse(cached[2].exists(), "unprotected entry must be evicted")
            self.assertEqual(
                {"1:2", "1:3"},
                {entry["track_key"] for entry in service.database.media_cache_entries()},
            )
            service.database.close()


if __name__ == "__main__":
    unittest.main()


class ArtworkEnrichmentTests(unittest.TestCase):
    """tracks_needing_artwork + miss markers + the autoArtwork setting."""

    def _database(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = Database(Path(directory.name) / "library.sqlite3")
        self.addCleanup(database.close)
        database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
        return database

    def _track(self, message_id, title="Song", artist="Artist"):
        return {
            "chatId": "1", "messageId": str(message_id), "fileName": f"song-{message_id}.mp3",
            "mimeType": "audio/mpeg", "title": title, "artist": artist,
        }

    def test_needing_artwork_is_oldest_first_and_respects_limit(self):
        database = self._database()
        for index in range(5):
            database.upsert_tracks([self._track(index)])
        result = database.tracks_needing_artwork(limit=2)
        self.assertEqual([item["key"] for item in result], ["1:0", "1:1"])
        result = database.tracks_needing_artwork(limit=10)
        self.assertEqual(len(result), 5)

    def test_needing_artwork_skips_edited_and_artworked_tracks(self):
        database = self._database()
        database.upsert_tracks([self._track(1), self._track(2), self._track(3), self._track(4)])
        database.save_metadata_patch("1", "1", {"title": "Edited"}, [])
        database.save_metadata_patch("1", "2", {"artworkPath": "abc.jpg"}, [])
        database.mark_artwork_miss("1:3")
        result = [item["key"] for item in database.tracks_needing_artwork(limit=10)]
        self.assertEqual(result, ["1:4"])

    def test_miss_marker_is_cleared_by_a_manual_edit(self):
        database = self._database()
        database.upsert_tracks([self._track(7)])
        database.mark_artwork_miss("1:7")
        self.assertEqual(database.tracks_needing_artwork(limit=10), [])
        # A manual edit excludes the track anyway (a human decided about it) ...
        database.save_metadata_patch("1", "7", {"title": "Human fix"}, [])
        self.assertEqual(database.tracks_needing_artwork(limit=10), [])
        # ... but reverting the edit must not resurrect the stale miss marker.
        database.save_metadata_patch("1", "7", {}, ["title"])
        self.assertEqual(len(database.tracks_needing_artwork(limit=10)), 1)

    def test_needing_artwork_skips_untitled_tracks(self):
        database = self._database()
        database.upsert_tracks([self._track(1, title="")])
        self.assertEqual(database.tracks_needing_artwork(limit=10), [])

    def test_auto_artwork_setting_defaults_on_and_validates_bool(self):
        database = self._database()
        self.assertTrue(database.get_settings()["autoArtwork"])
        database.save_settings({"autoArtwork": False})
        self.assertFalse(database.get_settings()["autoArtwork"])
        with self.assertRaises(ValueError):
            database.save_settings({"autoArtwork": "yes"})


class CursorPaginationTests(unittest.TestCase):
    """C2: keyset cursors walk a deep library without OFFSET scans."""

    def _database(self):
        database = Database(Path(tempfile.mkdtemp()) / "library.sqlite3")
        database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
        database.upsert_tracks([
            {"chatId": "1", "messageId": str(index), "fileName": f"song-{index}.mp3",
             "mimeType": "audio/mpeg", "title": f"Track {index}", "artist": "Artist",
             "sentAt": 1753000000 + index * 60}
            for index in range(500)
        ])
        return database

    def test_forward_and_backward_cursor_walks(self):
        database = self._database()
        try:
            first = database.list_tracks(limit=100)
            self.assertEqual(500, first["total"])
            self.assertIsNotNone(first["nextCursor"])
            self.assertIsNotNone(first["prevCursor"])
            page_keys = {item["key"] for item in first["items"]}

            second = database.list_tracks(limit=100, cursor=first["nextCursor"])
            self.assertEqual(100, len(second["items"]))
            self.assertFalse({item["key"] for item in second["items"]} & page_keys,
                              "cursor page must not overlap the previous one")
            self.assertEqual(second["items"][0]["sentAt"], first["items"][-1]["sentAt"] - 60)

            # Walking back with the previous page's prevCursor returns the same rows.
            back = database.list_tracks(limit=100, cursor=second["prevCursor"], before=True)
            self.assertEqual(100, len(back["items"]))
            self.assertEqual([item["key"] for item in back["items"]],
                             [item["key"] for item in first["items"]])
        finally:
            database.close()

    def test_cursor_tokens_are_absent_on_unsupported_paths(self):
        database = self._database()
        try:
            filtered = database.list_tracks(query="Track", limit=100)
            self.assertIsNone(filtered["nextCursor"], "searches fall back to OFFSET")
            self.assertIsNone(filtered["prevCursor"])
            source = database.list_tracks(chat_id="1", limit=100)
            self.assertIsNone(source["nextCursor"], "per-source views fall back to OFFSET")
        finally:
            database.close()


class QueryPerfTests(unittest.TestCase):
    """C1/C3/C4/C6: windowed queues, counts and day breaks never materialise the library."""

    def _database(self):
        database = Database(Path(tempfile.mkdtemp()) / "library.sqlite3")
        database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
        database.upsert_tracks([
            {"chatId": "1", "messageId": str(index), "fileName": f"song-{index}.mp3",
             "mimeType": "audio/mpeg", "title": f"Track {index}", "artist": "Artist",
             "sentAt": 1753000000 + index * 3600}
            for index in range(5000)
        ])
        return database

    def test_windowed_queue_fetches_only_the_window(self):
        database = self._database()
        statements = []

        def trace(*args):
            statements.append(args[0])

        try:
            database.connection.set_trace_callback(trace)
            result = database.playback_queue(
                current_key="1:2500", window_before=50, window_after=300
            )
            database.connection.set_trace_callback(None)
            self.assertEqual(351, len(result["keys"]))
            self.assertIn("1:2500", result["keys"])
            self.assertEqual(5000, result["total"])
            # No statement may pull every key into Python: the full-list SELECT shape
            # (no LIMIT) must not appear.
            for statement in statements:
                self.assertNotRegex(statement, r"SELECT t\.chat_id \|\| ':' \|\| t\.message_id AS key\s+FROM tracks t JOIN sources s [\s\S]*ORDER BY t\.sent_at DESC, t\.rowid DESC\s*$",
                                    "windowed queue must not materialise the full library")
                self.assertLess(len(statement), 500, "window queries stay small")
        finally:
            database.close()

    def test_counts_and_day_breaks_are_cached_per_generation(self):
        database = self._database()
        count_statements = []
        reader = database.reader
        # Only the filtered COUNT is what scrolls with the client; the allMusicTotal
        # COUNT is a separate cached query and would muddy the count.
        reader.set_trace_callback(
            lambda *args: count_statements.append(args[0])
            if args[0].strip().startswith("SELECT COUNT(*)") and "metadata_overrides" in args[0]
            else None
        )

        try:
            first = database.list_tracks(limit=100)
            self.assertEqual(5000, first["total"])
            self.assertEqual(5000, first["allMusicTotal"])
            self.assertGreater(len(first["dayBreaks"]), 0)
            # A second, deeper page with the same filter must reuse the cached values.
            second = database.list_tracks(limit=100, offset=4000)
            self.assertEqual(5000, second["total"])
            self.assertEqual(5000, second["allMusicTotal"])
            self.assertEqual(first["dayBreaks"], second["dayBreaks"],
                             "day breaks are recomputed only when the library changes")
            self.assertLessEqual(len(count_statements), 1,
                                 "only the first request pays for the counts")
            # A metadata edit changes search results: cached counts must be dropped.
            database.save_metadata_patch("1", "7", {"title": "Edited"}, [])
            count_statements.clear()
            third = database.list_tracks(query="Edited", limit=100)
            self.assertEqual(1, third["total"])
            self.assertEqual(1, len(count_statements), "the new generation re-counts")
        finally:
            reader.set_trace_callback(None)
            database.close()

    def test_fts_update_reuses_rowids_from_the_initial_query(self):
        database = self._database()
        lookups = []

        def trace(*args):
            if args[0].startswith("SELECT rowid FROM tracks WHERE"):
                lookups.append(args[0])

        try:
            database.connection.set_trace_callback(trace)
            database.list_tracks(query="Track 30")
            database.connection.set_trace_callback(None)
            self.assertEqual([], lookups, "FTS flush must not pay per-key rowid lookups")
            # "Track 4999" is unique in a 0..4999 crate; trigram phrase matching also
            # matches "Track 30" inside "Track 300", so a non-unique term would overcount.
            self.assertEqual(1, database.list_tracks(query="Track 4999")["total"])
        finally:
            database.close()


class RecordingIdentityTests(unittest.TestCase):
    """E1/E2/E3: durable recording identity, enrichment state, provenance."""

    def _database(self):
        return Database(Path(tempfile.mkdtemp()) / "library.sqlite3")

    def test_old_database_migrates_to_recording_schema(self):
        # Build a v11 database, then reopen: migrations 12-14 must apply without data loss.
        path = Path(tempfile.mkdtemp()) / "library.sqlite3"
        old = Database(path)
        old.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
        old.upsert_tracks([{
            "chatId": "1", "messageId": "2", "fileName": "song.mp3", "mimeType": "audio/mpeg",
            "title": "Old track", "artist": "Artist",
        }])
        old.close()
        fresh = Database(path)
        try:
            self.assertGreaterEqual(
                fresh.connection.execute("PRAGMA user_version").fetchone()[0], 14
            )
            self.assertEqual("Old track", fresh.get_track("1", "2")["metadata"]["title"])
        finally:
            fresh.close()

    def test_recording_identity_round_trip(self):
        database = self._database()
        try:
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            database.upsert_tracks([{
                "chatId": "1", "messageId": "2", "fileName": "song.mp3",
                "mimeType": "audio/mpeg", "title": "T", "artist": "A",
            }])
            recording_id = database.get_or_create_recording(
                musicbrainz_recording_id="mbid-1",
                canonical={"title": "Paranoid Android", "artist": "Radiohead", "year": 1997},
                confidence=0.98, method="acoustid", resolver_version=1,
            )
            # Same MBID returns the same recording.
            self.assertEqual(recording_id, database.get_or_create_recording(
                musicbrainz_recording_id="mbid-1"))
            database.link_track_recording("1:2", recording_id)
            recording = database.get_track_recording("1:2")
            self.assertIsNotNone(recording)
            self.assertEqual("mbid-1", recording["musicbrainz_recording_id"])
            self.assertEqual("Paranoid Android", recording["canonical_title"])
            self.assertEqual("1:2", database.get_track("1", "2")["key"])
        finally:
            database.close()

    def test_enrichment_state_and_stale_recovery(self):
        database = self._database()
        try:
            database.set_enrichment_state("1:2", "fingerprinting", fingerprint_version=1)
            state = database.get_enrichment_state("1:2")
            self.assertEqual("fingerprinting", state["status"])
            # A fresh state is not stale.
            self.assertEqual(0, database.reset_stale_enrichment_states(age_seconds=3600))
            # An old in-progress state becomes retryable.
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE track_enrichment SET last_attempt_at = 1 WHERE track_key = '1:2'"
                )
            self.assertEqual(1, database.reset_stale_enrichment_states(age_seconds=3600))
            state = database.get_enrichment_state("1:2")
            self.assertEqual("temporary_failure", state["status"])
            self.assertEqual("interrupted", state["failure_code"])
            # Terminal states are never recovered.
            database.set_enrichment_state("1:2", "resolved")
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE track_enrichment SET last_attempt_at = 1 WHERE track_key = '1:2'"
                )
            self.assertEqual(0, database.reset_stale_enrichment_states(age_seconds=3600))
        finally:
            database.close()

    def test_metadata_field_precedence_and_locks(self):
        database = self._database()
        try:
            recording_id = database.get_or_create_recording(musicbrainz_recording_id="mbid-1")
            # Automatic enrichment fills a field first.
            self.assertTrue(database.set_metadata_field(
                recording_id, "title", "Automatic title", source="fingerprint_resolver"))
            # A user correction outranks it.
            self.assertTrue(database.set_metadata_field(
                recording_id, "title", "User title", source="user", locked=True))
            # Automatic enrichment can never overwrite the locked user value.
            self.assertFalse(database.set_metadata_field(
                recording_id, "title", "Auto again", source="musicbrainz"))
            field = database.metadata_field(recording_id, "title")
            self.assertEqual("User title", field["value"])
            self.assertTrue(field["locked"])
            # A user-class write may update their own locked field.
            self.assertTrue(database.set_metadata_field(
                recording_id, "title", "Newer user title", source="user", locked=True))
            self.assertEqual("Newer user title",
                             database.metadata_field(recording_id, "title")["value"])
            # A lower-precedence source cannot overwrite a higher one even when unlocked.
            self.assertTrue(database.set_metadata_field(
                recording_id, "artist", "Auto artist", source="fingerprint_resolver"))
            self.assertFalse(database.set_metadata_field(
                recording_id, "artist", "Text guess", source="musicbrainz"))
            self.assertEqual("Auto artist", database.recording_metadata(recording_id)["artist"])
            # Unknown sources are rejected loudly.
            with self.assertRaises(ValueError):
                database.set_metadata_field(recording_id, "title", "x", source="spyware")
        finally:
            database.close()


class LikedSortTests(unittest.TestCase):
    """I1-I4: liked_at is durable, re-liking moves to the top, sorts are independent."""

    def _database(self):
        database = Database(Path(tempfile.mkdtemp()) / "library.sqlite3")
        database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
        database.upsert_tracks([
            {"chatId": "1", "messageId": str(i), "fileName": f"song-{i}.mp3",
             "mimeType": "audio/mpeg", "title": f"Track {i}", "artist": "Artist",
             "sentAt": 1753000000 + i * 60}
            for i in range(10)
        ])
        return database

    def test_like_response_carries_liked_at_and_relike_refreshes_it(self):
        database = self._database()
        try:
            with patch("core.now_ts", return_value=1000):
                result = database.set_liked("1:3", True)
            self.assertEqual({"liked": True, "likedAt": 1000}, result)
            with patch("core.now_ts", return_value=2000):
                database.set_liked("1:3", False)
                reliked = database.set_liked("1:3", True)
            self.assertEqual(2000, reliked["likedAt"],
                             "re-liking must get a new timestamp (back to the top)")
            self.assertTrue(database.get_track("1", "3")["liked"])
        finally:
            database.close()

    def test_liked_sort_orders_by_when_the_heart_was_pressed(self):
        database = self._database()
        try:
            # Like tracks out of posted order: 9 first, then 1.
            with patch("core.now_ts", return_value=100):
                database.set_liked("1:9", True)
            with patch("core.now_ts", return_value=200):
                database.set_liked("1:1", True)
            page = database.list_tracks(liked=True, sort="liked")
            self.assertEqual(["1:1", "1:9"], [item["key"] for item in page["items"]],
                             "recently liked first, independent of the posted order")
            old = database.list_tracks(liked=True, sort="liked_asc")
            self.assertEqual(["1:9", "1:1"], [item["key"] for item in old["items"]])
        finally:
            database.close()

    def test_library_sort_is_unaffected_by_liked_mode(self):
        database = self._database()
        try:
            with patch("core.now_ts", return_value=100):
                database.set_liked("1:5", True)
            library = database.list_tracks(sort="posted")
            self.assertEqual("1:9", library["items"][0]["key"],
                             "All Music keeps its own chronological order")
            liked = database.list_tracks(liked=True, sort="liked")
            self.assertEqual("1:5", liked["items"][0]["key"])
        finally:
            database.close()

    def test_liked_view_walks_with_keyset_cursors(self):
        database = Database(Path(tempfile.mkdtemp()) / "library.sqlite3")
        database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
        database.upsert_tracks([
            {"chatId": "1", "messageId": str(i), "fileName": f"song-{i}.mp3",
             "mimeType": "audio/mpeg", "title": f"Track {i}", "artist": "Artist",
             "sentAt": 1753000000 + i * 60}
            for i in range(60)
        ])
        try:
            for index in range(60):
                with patch("core.now_ts", return_value=100 + index):
                    database.set_liked(f"1:{index}", True)
            first = database.list_tracks(liked=True, sort="liked", limit=25)
            self.assertEqual(60, first["total"])
            self.assertIsNotNone(first["nextCursor"])
            second = database.list_tracks(liked=True, sort="liked", limit=25, cursor=first["nextCursor"])
            self.assertEqual(25, len(second["items"]))
            overlap = {item["key"] for item in first["items"]} & {item["key"] for item in second["items"]}
            self.assertEqual(set(), overlap)
            back = database.list_tracks(liked=True, sort="liked", limit=25, cursor=second["prevCursor"], before=True)
            self.assertEqual([item["key"] for item in first["items"]],
                             [item["key"] for item in back["items"]])
        finally:
            database.close()
