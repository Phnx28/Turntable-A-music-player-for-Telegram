"""Tests for external.py, the MusicBrainz / Cover Art Archive / LRCLIB client.

This module was untested. Network access is replaced with httpx.MockTransport rather than mocking
the methods under test, so request construction -- URL, query, User-Agent -- is asserted too.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from core import Database
from external import ExternalServices


def run(coro):
    return asyncio.run(coro)


class ExternalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.database = Database(root / "library.sqlite3")
        self.addCleanup(self.database.close)
        self.services = ExternalServices(self.database, root / "artwork", "test@example.com")
        self.addCleanup(lambda: run(self.services.close()))

    def stub_http(self, handler) -> list[httpx.Request]:
        """Point services.http at an in-process transport, returning a log of requests made."""
        seen: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        run(self.services.http.aclose())
        self.services.http = httpx.AsyncClient(transport=httpx.MockTransport(record))
        return seen


class LuceneEscapingTests(ExternalTestCase):
    """Track titles go into a MusicBrainz Lucene query; quotes and backslashes must not break out."""

    def test_quotes_are_escaped(self) -> None:
        self.assertEqual(ExternalServices._lucene('say "hi"'), 'say \\"hi\\"')

    def test_backslashes_are_escaped_before_quotes(self) -> None:
        # Order matters: escaping quotes first would leave the added backslash unescaped.
        self.assertEqual(ExternalServices._lucene('a\\b'), 'a\\\\b')

    def test_value_is_truncated(self) -> None:
        self.assertEqual(len(ExternalServices._lucene("x" * 400)), 250)


class UserAgentTests(ExternalTestCase):
    """MusicBrainz requires a contact in the User-Agent and blocks clients that omit one."""

    def test_contact_is_included(self) -> None:
        self.assertEqual(self.services.user_agent, "TelegramTurntable/1.0 (test@example.com)")

    def test_saved_setting_overrides_the_env_default(self) -> None:
        self.database.save_settings({"musicbrainzContact": "owner@example.org"})
        self.assertIn("owner@example.org", self.services.user_agent)

    def test_missing_contact_is_still_a_valid_header(self) -> None:
        services = ExternalServices(self.database, Path(self._tmp.name) / "art2", "")
        self.addCleanup(lambda: run(services.close()))
        self.assertEqual(services.user_agent, "TelegramTurntable/1.0 (configure-MusicBrainz-contact)")

    def test_test_musicbrainz_refuses_without_a_contact(self) -> None:
        services = ExternalServices(self.database, Path(self._tmp.name) / "art3", "")
        self.addCleanup(lambda: run(services.close()))
        with self.assertRaises(ValueError):
            run(services.test_musicbrainz())


class CoverUrlTests(ExternalTestCase):
    def test_quality_is_appended(self) -> None:
        url = "https://coverartarchive.org/release/abc/front"
        self.assertEqual(ExternalServices._cover_url(url, "500"), f"{url}-500")
        self.assertEqual(ExternalServices._cover_url(url, "1200"), f"{url}-1200")

    def test_original_drops_the_suffix(self) -> None:
        self.assertEqual(
            ExternalServices._cover_url("https://x/front-500", "original"), "https://x/front"
        )

    def test_existing_suffix_is_replaced_not_stacked(self) -> None:
        self.assertEqual(
            ExternalServices._cover_url("https://x/front-500", "1200"), "https://x/front-1200"
        )

    def test_unknown_quality_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ExternalServices._cover_url("https://x/front", "9999")


class ArtistCreditTests(ExternalTestCase):
    def test_join_phrases_are_preserved(self) -> None:
        credit = [
            {"name": "A", "joinphrase": " feat. "},
            {"name": "B", "joinphrase": ""},
        ]
        self.assertEqual(ExternalServices._artist_credit(credit), "A feat. B")

    def test_non_list_input_is_ignored(self) -> None:
        self.assertEqual(ExternalServices._artist_credit(None), "")
        self.assertEqual(ExternalServices._artist_credit("Artist"), "")

    def test_non_dict_entries_are_skipped(self) -> None:
        self.assertEqual(ExternalServices._artist_credit([{"name": "A"}, "junk"]), "A")


class MusicbrainzRequestTests(ExternalTestCase):
    def test_request_carries_the_user_agent_and_hits_the_ws2_endpoint(self) -> None:
        seen = self.stub_http(lambda request: httpx.Response(200, json={"recordings": []}))
        result = run(self.services._musicbrainz_get("/recording/", {"query": "x", "fmt": "json"}))
        self.assertEqual(result, {"recordings": []})
        self.assertEqual(len(seen), 1)
        self.assertEqual(str(seen[0].url).split("?")[0], "https://musicbrainz.org/ws/2/recording/")
        self.assertIn("test@example.com", seen[0].headers["user-agent"])
        self.assertEqual(seen[0].headers["accept"], "application/json")

    def test_http_errors_propagate(self) -> None:
        self.stub_http(lambda request: httpx.Response(503))
        with self.assertRaises(httpx.HTTPStatusError):
            run(self.services._musicbrainz_get("/recording/", {"query": "x"}))


def _jpeg_bytes() -> bytes:
    # Minimal, valid-enough JPEG payload: CAA only checks content-type and size limits,
    # and _download_cover writes whatever bytes arrive into the digest-named file.
    return b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


class EnrichTests(ExternalTestCase):
    """The auto cover-art policy: score gate, miss markers, error hygiene."""

    def _seed(self, message_id="5", title="Olivia Hope", artist="Olivia Rodrigo"):
        self.database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
        self.database.upsert_tracks([{
            "chatId": "1", "messageId": message_id, "fileName": f"track-{message_id}.mp3",
            "mimeType": "audio/mpeg", "title": title, "artist": artist, "durationMs": 1000,
        }])

    def _router(self, score: int = 97, with_cover: bool = True, cover_status: int = 200):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/ws/2/recording/" in url:
                recordings = []
                if with_cover:
                    recordings = [{
                        "id": "rec-1", "score": str(score),
                        "title": "Olivia Hope",
                        "artist-credit": [{"name": "Olivia Rodrigo", "joinphrase": ""}],
                        "length": 1000,
                        "releases": [{
                            "id": "rel-1", "status": "Official",
                            "release-group": {"id": "rg-1"}, "date": "2020-01-01",
                        }],
                    }]
                return httpx.Response(200, json={"recordings": recordings})
            if "coverartarchive.org" in url:
                if cover_status != 200:
                    return httpx.Response(cover_status)
                return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=_jpeg_bytes())
            return httpx.Response(404)
        return handler

    def _miss_marker(self, key: str = "1:5") -> bool:
        row = self.database.connection.execute(
            "SELECT 1 FROM artwork_misses WHERE track_key = ?", (key,)
        ).fetchone()
        return row is not None

    def test_score_96_never_applies_and_writes_a_miss(self):
        seen = self.stub_http(self._router(score=96))
        self._seed()
        result = run(self.services.enrich_covers())
        self.assertEqual(result, {"added": 0, "missed": 1})
        track = self.database.get_track("1", "5")
        self.assertFalse(track["metadata"].get("artworkPath"))
        self.assertEqual(len(seen), 1)  # one MusicBrainz lookup, no CAA fetch

    def test_score_97_with_cover_applies_art(self):
        self.stub_http(self._router(score=97, with_cover=True))
        self._seed()
        result = run(self.services.enrich_covers())
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["missed"], 0)
        track = self.database.get_track("1", "5")
        self.assertTrue(track["metadata"].get("artworkPath"))
        self.assertTrue((self.services.art_directory / track["metadata"]["artworkPath"]).is_file())

    def test_miss_marker_prevents_a_second_musicbrainz_query(self):
        seen = self.stub_http(self._router(score=40, with_cover=False))
        self._seed()
        run(self.services.enrich_covers())
        self.assertEqual(len(seen), 1)
        run(self.services.enrich_covers())
        self.assertEqual(len(seen), 1, "the miss marker must stop the repeat query")

    def test_network_error_does_not_write_a_miss(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)
        seen = self.stub_http(handler)
        self._seed()
        result = run(self.services.enrich_covers())
        # The first track raises (503 -> HTTPStatusError), the run stops, no miss marker.
        self.assertEqual(result, {"added": 0, "missed": 0})
        self.assertEqual(len(seen), 1)
        # Next run retries the same track.
        run(self.services.enrich_covers())
        self.assertEqual(len(seen), 2)

    def test_caa_404_writes_a_miss_marker(self):
        self.stub_http(self._router(cover_status=404))
        self._seed()
        result = run(self.services.enrich_covers())
        self.assertEqual(result, {"added": 0, "missed": 1})
        self.assertTrue(self._miss_marker())
        track = self.database.get_track("1", "5")
        self.assertFalse(track["metadata"].get("artworkPath"))
        # The marker makes the track ineligible for future runs.
        self.assertEqual(self.database.tracks_needing_artwork(), [])

    def test_caa_429_does_not_write_a_miss_marker(self):
        self.stub_http(self._router(cover_status=429))
        self._seed()
        result = run(self.services.enrich_covers())
        self.assertEqual(result, {"added": 0, "missed": 0})
        self.assertFalse(self._miss_marker())
        # The track stays eligible for the next run, which retries and succeeds.
        self.assertIn("5", [t["messageId"] for t in self.database.tracks_needing_artwork()])
        self.stub_http(self._router(cover_status=200))
        result = run(self.services.enrich_covers())
        self.assertEqual(result, {"added": 1, "missed": 0})
        track = self.database.get_track("1", "5")
        self.assertTrue(track["metadata"].get("artworkPath"))
        self.assertEqual(self.database.tracks_needing_artwork(), [])

    def test_caa_500_does_not_write_a_miss_marker(self):
        self.stub_http(self._router(cover_status=500))
        self._seed()
        result = run(self.services.enrich_covers())
        self.assertEqual(result, {"added": 0, "missed": 0})
        self.assertFalse(self._miss_marker())

    def test_cover_transport_error_does_not_write_a_miss_marker(self):
        base = self._router()

        def handler(request: httpx.Request) -> httpx.Response:
            if "coverartarchive.org" in str(request.url):
                raise httpx.ConnectError("connection refused")
            return base(request)

        self.stub_http(handler)
        self._seed()
        result = run(self.services.enrich_covers())
        self.assertEqual(result, {"added": 0, "missed": 0})
        self.assertFalse(self._miss_marker())
        # The track stays eligible for the next run.
        self.assertIn("5", [t["messageId"] for t in self.database.tracks_needing_artwork()])

    def test_successful_fetch_clears_a_stale_miss_marker(self):
        self.stub_http(self._router(cover_status=404))
        self._seed()
        run(self.services.enrich_covers())
        self.assertTrue(self._miss_marker())
        # A later fetch (the manual candidate dialog path) applies art and clears the miss.
        self.stub_http(self._router(cover_status=200))
        track = self.database.get_track("1", "5")
        run(self.services.apply_candidate(track, "rec-1:rel-1"))
        track = self.database.get_track("1", "5")
        self.assertTrue(track["metadata"].get("artworkPath"))
        self.assertFalse(self._miss_marker())
        self.assertEqual(self.database.tracks_needing_artwork(), [])

    def test_cover_error_classification(self):
        for status in (404, 410, 400, 403, 418):
            self.assertTrue(
                self.services._is_permanent_cover_miss(httpx.Response(status)), status
            )
        for status in (408, 425, 429, 500, 502, 503, 504):
            self.assertFalse(
                self.services._is_permanent_cover_miss(httpx.Response(status)), status
            )

    def test_retry_after_is_read_and_bounded(self):
        self.assertEqual(
            self.services._retry_after_seconds(httpx.Response(429, headers={"retry-after": "3"})), 3.0
        )
        # Above the cap the delay is clamped; absent or HTTP-date headers mean no wait.
        self.assertEqual(
            self.services._retry_after_seconds(httpx.Response(503, headers={"retry-after": "300"})), 60.0
        )
        self.assertEqual(self.services._retry_after_seconds(httpx.Response(429)), 0.0)
        self.assertEqual(
            self.services._retry_after_seconds(
                httpx.Response(429, headers={"retry-after": "Tue, 15 Nov 1994 08:12:31 GMT"})
            ),
            0.0,
        )

    def test_edited_and_artworked_tracks_are_untouched(self):
        self.stub_http(self._router(score=97, with_cover=True))
        self.database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
        self.database.upsert_tracks([
            {"chatId": "1", "messageId": "1", "fileName": "a.mp3", "mimeType": "audio/mpeg", "title": "A", "artist": "X"},
            {"chatId": "1", "messageId": "2", "fileName": "b.mp3", "mimeType": "audio/mpeg", "title": "B", "artist": "X"},
        ])
        self.database.save_metadata_patch("1", "1", {"title": "Human edit"}, [])
        self.database.save_metadata_patch("1", "2", {"artworkPath": "already.jpg"}, [])
        result = run(self.services.enrich_covers())
        self.assertEqual(result, {"added": 0, "missed": 0})

    def test_disabled_setting_and_missing_contact_no_op(self):
        self._seed()
        self.database.save_settings({"autoArtwork": False})
        result = run(self.services.enrich_covers())
        self.assertEqual(result["skipped"], "disabled")
        self.database.save_settings({"autoArtwork": True})
        services = ExternalServices(self.database, Path(self._tmp.name) / "art2", "")
        self.addCleanup(lambda: run(services.close()))
        result = run(services.enrich_covers())
        self.assertEqual(result["skipped"], "no-contact")
