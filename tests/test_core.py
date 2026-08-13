import asyncio
import datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core import (
    Database,
    RangeNotSatisfiable,
    now_ts,
    parse_lrc,
    parse_range_header,
    weighted_shuffle_tracks,
)
from cryptography.fernet import Fernet
from media import MEDIA_CHUNK_SIZE
from telegram_service import (
    QR_MODULE_RADIUS,
    QR_QUIET_MODULES,
    LoginFlow,
    TelegramService,
    render_qr_svg,
)
from telethon.errors import RPCError, SessionPasswordNeededError
from telethon.tl import functions
from telethon.tl.types import User
from telethon.tl.types import contacts as contacts_types


class CoreTests(unittest.TestCase):
    def test_ranges(self):
        value = parse_range_header("bytes=0-99", 1000)
        self.assertEqual((0, 99, True), (value.start, value.end, value.partial))
        self.assertEqual(
            (900, 999),
            (
                parse_range_header("bytes=-100", 1000).start,
                parse_range_header("bytes=-100", 1000).end,
            ),
        )
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
        self.assertGreaterEqual(
            QR_QUIET_MODULES, 3, "below this the code stops decoding"
        )
        # Corner rounding stays under half a module so modules remain squares, not pills.
        self.assertLess(QR_MODULE_RADIUS, 0.5)
        # Decoder-verified ceiling: .34 lost OpenCV decodes at 180px and 216px (the latter in
        # the real mobile render range) while .28 decoded every size; only raise it with a new
        # decode-harness pass.
        self.assertLessEqual(QR_MODULE_RADIUS, 0.28)
        # Every drawn module must sit within the quiet margin on all four sides.
        coordinates = [
            float(value) for value in re.findall(r"[MHV](-?\d+(?:\.\d+)?)", svg)
        ]
        drawn = [value for value in coordinates if value != 0.0]
        self.assertGreater(
            min(drawn), 0, "a module touches the edge, leaving no quiet zone"
        )
        self.assertLessEqual(max(drawn), extent - QR_QUIET_MODULES)

    def test_qr_solid_regions_stay_welded_not_pills(self):
        # A module fully surrounded by dark neighbours must be drawn as a plain square with no
        # arc commands: rounding every exposed corner would carve notches into solid regions
        # like the finder squares and blur them into isolated pills.
        import segno

        payload = "tg://login?token=AbCdEf0123456789"
        svg = render_qr_svg(payload)
        matrix = segno.make(payload).matrix
        size = len(matrix)
        for row in range(1, size - 1):
            for column in range(1, size - 1):
                if not (
                    matrix[row][column]
                    and matrix[row - 1][column]
                    and matrix[row + 1][column]
                    and matrix[row][column - 1]
                    and matrix[row][column + 1]
                ):
                    continue
                x, y = column + QR_QUIET_MODULES, row + QR_QUIET_MODULES
                # The renderer rounds a corner only where both adjoining neighbours are blank;
                # surrounded here on all four sides, the module must come out a perfect square.
                self.assertIn(f"M{x} {y}H{x + 1}V{y + 1}H{x}V{y}Z", svg)
                return
        self.fail("payload has no fully surrounded module to weld")

    def test_database_files_are_not_world_readable(self):
        # The database holds the Fernet-encrypted Telegram session and every chat title, so it
        # must not be left at the umask default. WAL sidecars carry the same pages.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "library.sqlite3"
            database = Database(path)
            database.list_sources(False)
            try:
                self.assertEqual(0o700, path.parent.stat().st_mode & 0o777)
                for target in (
                    path,
                    path.with_name(path.name + "-wal"),
                    path.with_name(path.name + "-shm"),
                ):
                    self.assertTrue(target.exists(), f"{target.name} missing")
                    self.assertEqual(
                        0,
                        target.stat().st_mode & 0o077,
                        f"{target.name} is group/world readable",
                    )
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
            track = {
                "chatId": "1",
                "messageId": "2",
                "fileName": "song.mp3",
                "mimeType": "audio/mpeg",
                "title": "Telegram title",
                "artist": "Telegram artist",
            }
            database.upsert_tracks([track])
            database.save_metadata_patch("1", "2", {"title": "My title"}, [])
            database.upsert_tracks([{**track, "title": "Changed upstream"}])
            self.assertEqual(
                "My title", database.get_track("1", "2")["metadata"]["title"]
            )
            self.assertEqual(
                "Changed upstream",
                database.get_track("1", "2")["telegramMetadata"]["title"],
            )
            database.close()

    def test_unselect_search_and_unlimited_library(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            tracks = [
                {
                    "chatId": "1",
                    "messageId": str(index),
                    "fileName": f"song-{index}.mp3",
                    "mimeType": "audio/mpeg",
                    "title": f"Telegram title {index}",
                    "artist": "Artist",
                }
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
            self.assertEqual(
                "1:2", database.list_tracks(query="Needle")["items"][0]["key"]
            )
            self.assertEqual(4998, database.track_position("1:2"))
            database.set_liked("1:2", True)
            self.assertEqual(
                ["1:2"],
                [item["key"] for item in database.list_tracks(liked=True)["items"]],
            )
            database.set_source_selected("1", False)
            self.assertEqual([], database.list_tracks()["items"])
            self.assertEqual(
                "1:2",
                database.list_tracks(query="Needle", include_unselected=True)["items"][
                    0
                ]["key"],
            )
            self.assertEqual(
                "Needle Remix", database.get_track("1", "2")["metadata"]["title"]
            )
            database.close()

    def test_library_rows_report_liked_the_same_as_queue_rows(self):
        # list_tracks omitted t.liked_at from its SELECT, so _track_summary's
        # value.get("liked_at") was always None and every library row rendered un-liked --
        # while the queue, which goes through track_summaries, showed the heart correctly.
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            database.upsert_tracks(
                [
                    {
                        "chatId": "1",
                        "messageId": "2",
                        "fileName": "song.mp3",
                        "mimeType": "audio/mpeg",
                        "title": "T",
                        "artist": "A",
                    }
                ]
            )
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
            database.upsert_tracks(
                [
                    {
                        "chatId": "1",
                        "messageId": "1",
                        "fileName": "a.mp3",
                        "mimeType": "audio/mpeg",
                        "title": "Zebra",
                    },
                    {
                        "chatId": "1",
                        "messageId": "2",
                        "fileName": "b.mp3",
                        "mimeType": "audio/mpeg",
                        "title": "Walrus",
                    },
                ]
            )
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
            database.upsert_tracks(
                [
                    {
                        "chatId": "1",
                        "messageId": "1",
                        "fileName": "z.mp3",
                        "mimeType": "audio/mpeg",
                        "title": "Zebra",
                        "artist": "Beta",
                        "durationMs": 300_000,
                        "sentAt": 300,
                    },
                    {
                        "chatId": "1",
                        "messageId": "2",
                        "fileName": "a.mp3",
                        "mimeType": "audio/mpeg",
                        "title": "apple",
                        "artist": "Alpha",
                        "durationMs": 100_000,
                        "sentAt": 200,
                    },
                    {
                        "chatId": "1",
                        "messageId": "3",
                        "fileName": "m.mp3",
                        "mimeType": "audio/mpeg",
                        "title": "Mango",
                        "artist": "Gamma",
                        "durationMs": 200_000,
                        "sentAt": 100,
                    },
                ]
            )
            # The displayed title wins over the Telegram one, so sorting must follow the override.
            database.save_metadata_patch("1", "1", {"title": "Aardvark override"}, [])

            def keys(sort):
                return [
                    item["key"] for item in database.list_tracks(sort=sort)["items"]
                ]

            self.assertEqual(["1:1", "1:2", "1:3"], keys("posted"))
            # Aardvark override first, then apple -- COLLATE NOCASE, or "apple" would follow "Mango".
            self.assertEqual(["1:1", "1:2", "1:3"], keys("title"))
            self.assertEqual(["1:2", "1:1", "1:3"], keys("artist"))
            self.assertEqual(["1:1", "1:3", "1:2"], keys("duration"))

            # Anything not on the allowlist degrades to posted rather than reaching SQL.
            for hostile in [
                "title; DROP TABLE tracks",
                "t.sent_at ASC",
                "",
                "nonsense",
                None,
            ]:
                self.assertEqual(
                    keys("posted"), keys(hostile), f"{hostile!r} was not rejected"
                )
            # And the table is still there.
            self.assertEqual(3, database.list_tracks()["total"])
            database.close()

    def test_track_pages_report_authoritative_all_music_total_and_utc_day_breaks(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source(
                {"chatId": "1", "kind": "channel", "title": "Selected"}
            )
            database.upsert_source(
                {"chatId": "2", "kind": "channel", "title": "Hidden"}
            )
            database.upsert_tracks(
                [
                    {
                        "chatId": "1",
                        "messageId": "1",
                        "fileName": "a.mp3",
                        "mimeType": "audio/mpeg",
                        "title": "Needle",
                        "sentAt": 1753835400,
                    },
                    {
                        "chatId": "1",
                        "messageId": "2",
                        "fileName": "b.mp3",
                        "mimeType": "audio/mpeg",
                        "title": "Other",
                        "sentAt": 1753749000,
                    },
                    {
                        "chatId": "1",
                        "messageId": "3",
                        "fileName": "c.mp3",
                        "mimeType": "audio/mpeg",
                        "title": "Unknown date",
                        "sentAt": 0,
                    },
                    {
                        "chatId": "2",
                        "messageId": "1",
                        "fileName": "d.mp3",
                        "mimeType": "audio/mpeg",
                        "title": "Hidden",
                        "sentAt": 1753835400,
                    },
                ]
            )
            database.set_source_selected("2", False)

            page = database.list_tracks(query="Needle")
            self.assertEqual(
                1, page["total"], "active-view total remains query-specific"
            )
            self.assertEqual(
                3, page["allMusicTotal"], "selected, available tracks are authoritative"
            )
            self.assertEqual([{"index": 0, "dayKey": "2025-07-30"}], page["dayBreaks"])

            full = database.list_tracks()
            self.assertEqual(
                [
                    {"index": 0, "dayKey": "2025-07-30"},
                    {"index": 1, "dayKey": "2025-07-29"},
                ],
                full["dayBreaks"],
                "invalid/non-positive timestamps do not create rules",
            )
            database.mark_unavailable("1", ["2"])
            self.assertEqual(2, database.list_tracks()["allMusicTotal"])

            for kwargs in ({"chat_id": "1"}, {"liked": True}, {"sort": "title"}):
                self.assertEqual([], database.list_tracks(**kwargs)["dayBreaks"])
            database.close()

    def test_track_position_matches_each_allowlisted_track_sort(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            database.upsert_tracks(
                [
                    {
                        "chatId": "1",
                        "messageId": "1",
                        "fileName": "z.mp3",
                        "mimeType": "audio/mpeg",
                        "title": "Zebra",
                        "artist": "Beta",
                        "durationMs": 300_000,
                        "sentAt": 300,
                    },
                    {
                        "chatId": "1",
                        "messageId": "2",
                        "fileName": "a.mp3",
                        "mimeType": "audio/mpeg",
                        "title": "apple",
                        "artist": "Alpha",
                        "durationMs": 100_000,
                        "sentAt": 200,
                    },
                    {
                        "chatId": "1",
                        "messageId": "3",
                        "fileName": "m.mp3",
                        "mimeType": "audio/mpeg",
                        "title": "Mango",
                        "artist": "Gamma",
                        "durationMs": 200_000,
                        "sentAt": 100,
                    },
                ]
            )
            database.save_metadata_patch("1", "1", {"title": "Aardvark override"}, [])

            self.assertEqual(
                0, database.track_position("1:1", chat_id="1", sort="posted")
            )
            self.assertEqual(
                1, database.track_position("1:2", chat_id="1", sort="title")
            )
            self.assertEqual(
                1, database.track_position("1:1", chat_id="1", sort="artist")
            )
            self.assertEqual(
                2, database.track_position("1:2", chat_id="1", sort="duration")
            )
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
            database.upsert_source(
                {"chatId": "2", "kind": "channel", "title": "Dropped"}
            )
            database.upsert_tracks(
                [
                    {
                        "chatId": "1",
                        "messageId": "1",
                        "fileName": "a.mp3",
                        "mimeType": "audio/mpeg",
                    },
                    {
                        "chatId": "2",
                        "messageId": "1",
                        "fileName": "b.mp3",
                        "mimeType": "audio/mpeg",
                    },
                    {
                        "chatId": "2",
                        "messageId": "2",
                        "fileName": "c.mp3",
                        "mimeType": "audio/mpeg",
                    },
                ]
            )
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
            {
                "key": str(index),
                "playCount": index,
                "lastStartedAt": current - index if index < 20 else 0,
                "lastPlayedAt": 0,
            }
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
            service.flows["flow"] = LoginFlow(
                "flow", "phone", client, state="password_required"
            )
            for _ in range(2):
                result = await service.submit_password("flow", "wrong")
                self.assertEqual("password_required", result["state"])
                self.assertEqual(
                    "The Telegram 2FA password is incorrect", result["error"]
                )
            self.assertEqual(2, client.password_attempts)
            service.database.close()

    async def test_cancelled_preview_saves_tracks_without_advancing_the_cursor(self):
        # Browsing away cancels the preview job. Keep whatever was already read, but leave
        # lastMessageId alone: iter_messages walks newest to oldest, so advancing it after a
        # partial scan would make the next incremental sync skip every older message.
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            service.database.upsert_source(
                {"chatId": "55", "kind": "channel", "title": "Preview"}
            )
            messages = [SimpleNamespace(id=index) for index in (40, 30, 20, 10)]
            service.client = FakeCancellingSyncClient(messages, cancel_after=2)
            service._message_to_track = lambda message, chat_id: {
                "chatId": chat_id,
                "messageId": str(message.id),
                "fileName": f"{message.id}.mp3",
                "mimeType": "audio/mpeg",
            }
            with self.assertRaises(asyncio.CancelledError):
                await service.sync_source("55", full=True, temporary=True)
            source = service.database.get_source("55")
            self.assertEqual(
                0, int(source["lastMessageId"] or 0), "cursor must not advance"
            )
            self.assertEqual(2, service.database.list_tracks(chat_id="55")["total"])
            service.database.close()

    async def test_completed_sync_advances_the_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            service.database.upsert_source(
                {"chatId": "56", "kind": "channel", "title": "Done"}
            )
            messages = [SimpleNamespace(id=index) for index in (40, 30, 20, 10)]
            service.client = FakeCancellingSyncClient(messages, cancel_after=None)
            service._message_to_track = lambda message, chat_id: {
                "chatId": chat_id,
                "messageId": str(message.id),
                "fileName": f"{message.id}.mp3",
                "mimeType": "audio/mpeg",
            }
            await service.sync_source("56", full=True, temporary=True)
            self.assertEqual(
                40, int(service.database.get_source("56")["lastMessageId"] or 0)
            )
            self.assertEqual(4, service.database.list_tracks(chat_id="56")["total"])
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
                "chatId": "22",
                "messageId": "7",
                "fileName": "result.mp3",
                "mimeType": "audio/mpeg",
                "title": "Search result",
                "artist": "Artist",
            }
            first = await asyncio.wait_for(service.global_music_search("result", 10), 2)
            second = await asyncio.wait_for(
                service.global_music_search("result", 10), 2
            )
            self.assertEqual(2, client.searches)
            self.assertEqual("22:7", first["tracks"][0]["key"])
            self.assertEqual("22:7", second["tracks"][0]["key"])
            self.assertFalse(service.database.get_source("22")["selected"])
            self.assertEqual(
                "Result archive", service.database.get_source("23")["title"]
            )
            service.database.close()

    async def test_tagged_download_uses_local_metadata_without_touching_original(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            original = Path(directory) / "original.audio"
            original.write_bytes(b"original")
            service.media.cache_media = AsyncMock(return_value=original)
            track = {
                "key": "1:2",
                "chatId": "1",
                "messageId": "2",
                "documentId": "3",
                "file": {"name": "song.mp3", "size": 8},
                "metadata": {"title": "Edited title", "artist": "Edited artist"},
                "overrides": {"title": "Edited title"},
            }
            commands = []

            async def fake_exec(*command, **_):
                commands.append(command)
                Path(command[-1]).write_bytes(b"tagged")
                return SimpleNamespace(
                    returncode=0, communicate=AsyncMock(return_value=(b"", b""))
                )

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
                "key": "1:2",
                "chatId": "1",
                "messageId": "2",
                "documentId": "3",
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
                "key": "1:2",
                "chatId": "1",
                "messageId": "2",
                "documentId": "3",
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
                "key": "1:2",
                "chatId": "1",
                "messageId": "2",
                "documentId": "9",
                "file": {"name": "song.mp3", "size": total},
            }
            media.get_message_document = AsyncMock(
                return_value=(None, SimpleNamespace(id=9, size=total))
            )

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
            media.get_message_document = AsyncMock(
                return_value=(None, SimpleNamespace(id=10, size=total))
            )

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
            removed = media.clear_cache()
            self.assertGreaterEqual(removed["removedBytes"], total + len(b"partial"))
            self.assertEqual([], list(media.media_directory.iterdir()))
            service.database.close()

    async def test_sync_source_runs_per_source_in_parallel_and_caps_total(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            # 4 sources, semaphore(3) means 3 run concurrently and the 4th waits.
            for chat_id in ("1", "2", "3", "4"):
                service.database.upsert_source(
                    {
                        "chatId": chat_id,
                        "kind": "channel",
                        "title": f"S{chat_id}",
                        "selected": True,
                    }
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

            service.database.get_source = lambda chat_id: {
                "chatId": chat_id,
                "selected": True,
                "lastMessageId": 0,
            }
            service.require_client = lambda: SimpleNamespace(
                is_connected=lambda: True,
                get_entity=AsyncMock(side_effect=fake_get_entity),
                iter_messages=fake_iter_messages,
            )
            tasks = [
                asyncio.create_task(service.sync_source(chat_id))
                for chat_id in ("1", "2", "3", "4")
            ]
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
            service.database.get_source = lambda _chat_id: {
                "chatId": "1",
                "selected": True,
                "lastMessageId": 0,
            }

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
            self.assertLessEqual(
                peak, 1, "per-source lock must serialize the same chat_id"
            )
            service.database.close()


class FakeQrClient:
    """Minimal client surface _wait_for_qr/_complete_flow touch; never talks to Telegram."""

    def __init__(self):
        self.disconnects = 0
        self.session = SimpleNamespace(save=lambda: "1" + "A" * 32)
        self.connect = AsyncMock()
        self.qr_login = AsyncMock()

    def add_event_handler(self, *_, **__):
        pass

    async def disconnect(self):
        self.disconnects += 1

    async def log_out(self):
        self.disconnects += 1

    async def get_me(self):
        return User(id=123, first_name="Test", last_name=None, username="test")


class QrLoginFlowTests(unittest.IsolatedAsyncioTestCase):
    """Characterize _wait_for_qr state transitions with fakes; no Telegram connection."""

    def service(self, directory: str) -> TelegramService:
        return TelegramService(
            Database(Path(directory) / "library.sqlite3"),
            api_id=1,
            api_hash="test",
            encryption_key=Fernet.generate_key().decode(),
            data_directory=Path(directory),
        )

    def make_flow(self, qr_wait: AsyncMock) -> LoginFlow:
        client = FakeQrClient()
        flow = LoginFlow("flow", "qr", client)
        flow.qr = SimpleNamespace(
            wait=qr_wait,
            expires=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=30),
        )
        return flow

    async def test_qr_wait_success_reaches_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            service.jobs.spawn = lambda coroutine: coroutine.close()  # sync_all must not run in the test loop
            flow = self.make_flow(AsyncMock(return_value=None))
            await service._wait_for_qr(flow)
            self.assertEqual("ready", flow.state)
            self.assertEqual("", flow.error)
            account = service.database.get_account()
            assert account is not None
            self.assertEqual("123", account["telegram_user_id"])
            self.assertIs(flow.client, service.client)
            service.database.close()

    async def test_qr_wait_password_needed_marks_password_required(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            flow = self.make_flow(
                AsyncMock(side_effect=SessionPasswordNeededError(None))
            )
            await service._wait_for_qr(flow)
            self.assertEqual("password_required", flow.state)
            self.assertIsNone(service.database.get_account())
            service.database.close()

    async def test_qr_wait_timeout_marks_expired_and_disconnects(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            flow = self.make_flow(AsyncMock(side_effect=asyncio.TimeoutError()))
            await service._wait_for_qr(flow)
            self.assertEqual("expired", flow.state)
            self.assertEqual(1, flow.client.disconnects)
            service.database.close()

    async def test_qr_wait_generic_error_marks_error_with_friendly_text(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            flow = self.make_flow(
                AsyncMock(side_effect=RuntimeError("PHONE_CODE_INVALID"))
            )
            await service._wait_for_qr(flow)
            self.assertEqual("error", flow.state)
            self.assertEqual("The Telegram login code is incorrect", flow.error)
            service.database.close()


    async def test_qr_wait_timeout_derives_from_token_expiry(self):
        # The wait must track the token's own lifetime. A hardcoded 60s can outlive a
        # short-lived token (Telegram shows "invalid/expired" while Turntable still waits)
        # or undershoot it, so the timeout passed to qr.wait must come from qr.expires.
        import datetime

        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            wait = AsyncMock(side_effect=asyncio.TimeoutError())
            flow = self.make_flow(wait)
            flow.qr.expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=10)
            await service._wait_for_qr(flow)
            self.assertEqual("expired", flow.state)
            wait.assert_awaited_once()
            self.assertIsNotNone(wait.await_args)
            assert wait.await_args is not None
            timeout = wait.await_args.kwargs.get("timeout")
            self.assertIsNotNone(timeout, "timeout must be passed to qr.wait")
            assert timeout is not None
            self.assertGreaterEqual(timeout, 9.0)
            self.assertLess(timeout, 60.0, "timeout must come from the token expiry, not a hardcoded 60s")
            service.database.close()

    async def test_start_qr_login_starts_the_wait_task_before_returning(self):
        # Branch C guard: the wait task must be alive before the QR SVG is handed to the
        # browser, otherwise a scanned QR can never complete the login.
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            client = FakeQrClient()
            gate = asyncio.Event()
            fake_qr = SimpleNamespace(
                url="tg://login?token=TestToken",
                expires=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=30),
                wait=lambda timeout: gate.wait(),
            )
            client.qr_login.return_value = fake_qr
            service._new_client = lambda session="": client
            service.jobs.spawn = lambda coroutine: coroutine.close()
            result = await service.start_qr_login()
            flow = service.flows[result["flowId"]]
            self.assertIsNotNone(flow.task)
            task = flow.task
            assert task is not None
            self.assertFalse(task.done())
            self.assertIn("viewBox", result["svg"])
            # Let the waiter finish cleanly so the test loop has no pending tasks.
            gate.set()
            await task
            self.assertEqual("ready", flow.state)
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
            User(
                id=1,
                first_name="Ada",
                username="ada",
                bot=False,
                deleted=False,
                is_self=False,
            ),
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
        peers = SimpleNamespace(
            peers=[
                SimpleNamespace(peer=SimpleNamespace(user_id=3), rating=9.0),
                SimpleNamespace(peer=SimpleNamespace(user_id=99), rating=8.0),
                SimpleNamespace(peer=SimpleNamespace(user_id=1), rating=2.0),
                SimpleNamespace(peer=SimpleNamespace(user_id=42), rating=1.0),
            ]
        )
        result = contacts_types.TopPeers(
            categories=[SimpleNamespace(category=None, peers=peers.peers)],
            chats=[],
            users=[],
        )
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory)
            service.require_client = lambda: self._client(result)
            people = await service.contacts()
            self.assertEqual(["Ada", "Bo", "Cy"], [item["name"] for item in people])
            self.assertEqual(
                {"Ada": 2.0, "Bo": None, "Cy": 9.0},
                {item["name"]: item["forwardRank"] for item in people},
            )
            service.database.close()

    async def test_disabled_or_failing_top_peers_still_returns_contacts(self):
        # Frequent contacts are a nicety. A user who turned off suggestions, or a flood wait,
        # must not take down the whole share picker.
        for outcome in (
            contacts_types.TopPeersDisabled(),
            RPCError("req", "FLOOD_WAIT_5", 420),
        ):
            with tempfile.TemporaryDirectory() as directory:
                service = self.service(directory)
                service.require_client = lambda outcome=outcome: self._client(outcome)
                people = await service.contacts()
                self.assertEqual(["Ada", "Bo", "Cy"], [item["name"] for item in people])
                self.assertTrue(
                    all(item["forwardRank"] is None for item in people),
                    f"{type(outcome).__name__} must degrade to an unranked list",
                )
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
            database.upsert_tracks(
                [
                    {
                        "chatId": "1",
                        "messageId": str(index),
                        "fileName": f"song-{index}.mp3",
                        "mimeType": "audio/mpeg",
                        "title": f"Track {index}",
                        "artist": "Artist",
                    }
                    for index in range(5001)
                ]
            )
            try:
                self.assertEqual(5001, len(database.playback_queue("1")))
            finally:
                database.close()

    def test_window_slices_around_the_current_track(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            database.upsert_tracks(
                [
                    {
                        "chatId": "1",
                        "messageId": str(index),
                        "fileName": f"song-{index}.mp3",
                        "mimeType": "audio/mpeg",
                        "title": f"Track {index}",
                        "artist": "Artist",
                    }
                    for index in range(5001)
                ]
            )
            try:
                full = self._full(database, 5001)
                current = "1:2500"
                result = database.playback_queue(
                    current_key=current, window_before=50, window_after=300
                )
                self.assertIsInstance(result, dict)
                assert isinstance(result, dict)
                self.assertEqual(5001, result["total"])
                self.assertEqual(2450, result["offset"])
                # The window is a contiguous slice of the full ordering, current track inside.
                self.assertEqual(
                    full[result["offset"] : result["offset"] + len(result["keys"])],
                    result["keys"],
                )
                self.assertEqual(current, result["keys"][50])
                self.assertEqual(351, len(result["keys"]))
                # Slices must never reach past the ends of the library.
                first = database.playback_queue(
                    current_key="1:4999", window_before=50, window_after=300
                )
                assert isinstance(first, dict)
                self.assertEqual(0, first["offset"])
                self.assertEqual("1:5000", first["keys"][0])
                self.assertIn("1:4999", first["keys"][:2])
                last = database.playback_queue(
                    current_key="1:0", window_before=50, window_after=300
                )
                assert isinstance(last, dict)
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
            database.upsert_tracks(
                [
                    {
                        "chatId": "1",
                        "messageId": str(index),
                        "fileName": f"song-{index}.mp3",
                        "mimeType": "audio/mpeg",
                        "title": f"Track {index}",
                        "artist": "Artist",
                    }
                    for index in range(50)
                ]
            )
            try:
                full = database.playback_queue("1", shuffle=True, current_key="1:25")
                result = database.playback_queue(
                    "1",
                    shuffle=True,
                    current_key="1:25",
                    window_before=5,
                    window_after=5,
                )
                assert isinstance(result, dict)
                self.assertEqual(len(full), result["total"])
                self.assertEqual(6, len(result["keys"]))
                self.assertEqual(0, result["offset"])
                self.assertNotIn("1:25", result["keys"])
                self.assertEqual(len(result["keys"]), len(set(result["keys"])))
                self.assertLessEqual(result["offset"] + len(result["keys"]), 50)
            finally:
                database.close()


class SearchReconcileTests(unittest.TestCase):
    def test_reconcile_rebuilds_a_drifted_fts_index(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            database.upsert_tracks(
                [
                    {
                        "chatId": "1",
                        "messageId": str(index),
                        "fileName": f"song-{index}.mp3",
                        "mimeType": "audio/mpeg",
                        "title": f"Track {index}",
                        "artist": "Artist",
                    }
                    for index in range(40)
                ]
            )
            # Simulate a crash that dropped the in-memory dirty set: rows missing from FTS and
            # no pending flush left to restore them. Any search between upsert and flush would
            # have re-inserted them, so clear the set to mimic process death.
            database._dirty_search_keys.clear()
            with database.transaction() as connection:
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
            database.upsert_tracks(
                [
                    {
                        "chatId": "1",
                        "messageId": str(index),
                        "fileName": f"song-{index}.mp3",
                        "mimeType": "audio/mpeg",
                        "title": f"Track {index}",
                        "artist": "Artist",
                    }
                    for index in range(10)
                ]
            )
            # The dirty set is the index's working memory: flush it, then the counts agree and
            # reconcile must find nothing to do.
            database._flush_search()
            self.assertEqual(0, database.reconcile_search())
            database.close()


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
            "chatId": "1",
            "messageId": str(message_id),
            "fileName": f"song-{message_id}.mp3",
            "mimeType": "audio/mpeg",
            "title": title,
            "artist": artist,
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
        database.upsert_tracks(
            [self._track(1), self._track(2), self._track(3), self._track(4)]
        )
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
