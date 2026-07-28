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
