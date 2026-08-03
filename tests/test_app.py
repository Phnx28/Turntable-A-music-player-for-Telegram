"""Tests for the HTTP layer in app.py.

app.py had no tests at all: 62 routes, plus the middleware that enforces CSRF and the password
gate. These cover the middleware and the unauthenticated surface, which is the part a broken
change would expose rather than merely break.

TelegramService.start() is patched out throughout. It opens a real Telegram connection, and the
lifespan runs it on startup, so an unpatched TestClient would reach the network.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import SESSION_COOKIE, Settings, create_app
from core import media_digest, media_identity


def _settings(data_directory: Path) -> Settings:
    return Settings(
        api_id=12345,
        api_hash="0123456789abcdef0123456789abcdef",
        encryption_key=Fernet.generate_key().decode(),
        data_directory=data_directory,
        musicbrainz_contact="",
    )


class AppTestCase(unittest.TestCase):
    """Base class giving each test a throwaway data directory and a started app."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch("app.TelegramService.start", new=AsyncMock(return_value=None))
        patcher.start()
        self.addCleanup(patcher.stop)
        stopper = patch("app.TelegramService.stop", new=AsyncMock(return_value=None))
        stopper.start()
        self.addCleanup(stopper.stop)
        self.app = create_app(_settings(Path(self._tmp.name)))
        self.client_cm = TestClient(self.app)
        self.client = self.client_cm.__enter__()
        self.addCleanup(self.client_cm.__exit__, None, None, None)

    @property
    def database(self):
        return self.app.state.database


class HealthTests(AppTestCase):
    def test_healthz_reports_database_and_telegram_state(self) -> None:
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["database"])
        # start() is stubbed, so the service never links.
        self.assertFalse(body["telegram"])

    def test_security_headers_are_present(self) -> None:
        response = self.client.get("/healthz")
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")


class CsrfTests(AppTestCase):
    """The middleware fails closed: a state-changing /api/ call must prove same-origin intent."""

    def test_post_without_origin_or_fetch_site_is_rejected(self) -> None:
        response = self.client.patch("/api/settings", json={"prefetchCount": 3})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "forbidden")

    def test_post_with_foreign_origin_is_rejected(self) -> None:
        response = self.client.patch(
            "/api/settings", json={"prefetchCount": 3}, headers={"origin": "http://evil.example"}
        )
        self.assertEqual(response.status_code, 403)

    def test_post_with_cross_site_fetch_site_is_rejected(self) -> None:
        response = self.client.patch(
            "/api/settings", json={"prefetchCount": 3}, headers={"sec-fetch-site": "cross-site"}
        )
        self.assertEqual(response.status_code, 403)

    def test_same_origin_fetch_site_is_allowed(self) -> None:
        response = self.client.patch(
            "/api/settings", json={"prefetchCount": 3}, headers={"sec-fetch-site": "same-origin"}
        )
        self.assertEqual(response.status_code, 200)

    def test_get_requests_are_not_origin_checked(self) -> None:
        # Reads are safe and the frontend issues them without these headers.
        self.assertEqual(self.client.get("/api/settings").status_code, 200)


class PasswordGateTests(AppTestCase):
    """With no password set the app is open; once set, /api/ needs a valid session cookie."""

    SAME_ORIGIN = {"sec-fetch-site": "same-origin"}

    def test_api_is_open_when_no_password_is_set(self) -> None:
        self.assertEqual(self.client.get("/api/settings").status_code, 200)

    def test_api_requires_a_session_once_a_password_is_set(self) -> None:
        self.database.set_password("correct horse battery")
        response = self.client.get("/api/settings")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_public_paths_stay_reachable_when_locked(self) -> None:
        self.database.set_password("correct horse battery")
        # Without these the user could never reach the login screen to unlock the app.
        self.assertEqual(self.client.get("/api/auth/status").status_code, 200)
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_login_with_the_right_password_unlocks_the_api(self) -> None:
        self.database.set_password("correct horse battery")
        login = self.client.post(
            "/api/auth/login", json={"password": "correct horse battery"}, headers=self.SAME_ORIGIN
        )
        self.assertEqual(login.status_code, 200)
        self.assertIn(SESSION_COOKIE, login.cookies)
        self.assertEqual(self.client.get("/api/settings").status_code, 200)

    def test_login_with_the_wrong_password_is_rejected(self) -> None:
        self.database.set_password("correct horse battery")
        login = self.client.post(
            "/api/auth/login", json={"password": "wrong"}, headers=self.SAME_ORIGIN
        )
        self.assertEqual(login.status_code, 401)
        self.assertNotIn(SESSION_COOKIE, login.cookies)
        self.assertEqual(self.client.get("/api/settings").status_code, 401)

    def test_a_forged_session_cookie_is_rejected(self) -> None:
        self.database.set_password("correct horse battery")
        self.client.cookies.set(SESSION_COOKIE, "not-a-real-token")
        self.assertEqual(self.client.get("/api/settings").status_code, 401)

    def test_logout_relocks_the_api(self) -> None:
        self.database.set_password("correct horse battery")
        self.client.post(
            "/api/auth/login", json={"password": "correct horse battery"}, headers=self.SAME_ORIGIN
        )
        self.assertEqual(self.client.get("/api/settings").status_code, 200)
        self.assertEqual(
            self.client.post("/api/auth/logout", headers=self.SAME_ORIGIN).status_code, 200
        )
        self.assertEqual(self.client.get("/api/settings").status_code, 401)

    def test_login_is_rate_limited_after_repeated_failures(self) -> None:
        # The throttle is module-level in-memory, so other tests can leave failures behind --
        # and this test's own failures must not leak into them either.
        from app import _clear_login_failures

        try:
            _clear_login_failures("testclient")
            self.database.set_password("correct horse battery")
            for _ in range(5):
                response = self.client.post(
                    "/api/auth/login", json={"password": "wrong"}, headers=self.SAME_ORIGIN
                )
                self.assertEqual(response.status_code, 401)
            blocked = self.client.post(
                "/api/auth/login",
                json={"password": "correct horse battery"},
                headers=self.SAME_ORIGIN,
            )
            self.assertEqual(blocked.status_code, 429)
            self.assertEqual(blocked.json()["error"]["code"], "rate_limited")
        finally:
            _clear_login_failures("testclient")

    def test_changing_the_password_signs_other_sessions_out(self) -> None:
        # The README claims this; it happens via an inline DELETE in set_password.
        self.database.set_password("first password")
        self.client.post(
            "/api/auth/login", json={"password": "first password"}, headers=self.SAME_ORIGIN
        )
        self.assertEqual(self.client.get("/api/settings").status_code, 200)
        self.database.set_password("second password")
        self.assertEqual(self.client.get("/api/settings").status_code, 401)


class SettingsRouteTests(AppTestCase):
    SAME_ORIGIN = {"sec-fetch-site": "same-origin"}

    def test_get_returns_defaults(self) -> None:
        body = self.client.get("/api/settings").json()
        self.assertIn("coverQuality", body)
        self.assertIn("prefetchCount", body)

    def test_patch_persists_and_round_trips(self) -> None:
        self.client.patch(
            "/api/settings", json={"coverQuality": "original"}, headers=self.SAME_ORIGIN
        )
        self.assertEqual(self.client.get("/api/settings").json()["coverQuality"], "original")

    def test_patch_rejects_an_unknown_cover_quality(self) -> None:
        response = self.client.patch(
            "/api/settings", json={"coverQuality": "enormous"}, headers=self.SAME_ORIGIN
        )
        self.assertEqual(response.status_code, 422)

    def test_patch_rejects_an_out_of_range_prefetch_count(self) -> None:
        response = self.client.patch(
            "/api/settings", json={"prefetchCount": 999}, headers=self.SAME_ORIGIN
        )
        self.assertEqual(response.status_code, 422)

    def test_omitted_fields_are_left_alone(self) -> None:
        # exclude_none in the handler: a partial PATCH must not blank the other settings.
        self.client.patch(
            "/api/settings", json={"coverQuality": "500"}, headers=self.SAME_ORIGIN
        )
        self.client.patch(
            "/api/settings", json={"prefetchCount": 2}, headers=self.SAME_ORIGIN
        )
        body = self.client.get("/api/settings").json()
        self.assertEqual(body["coverQuality"], "500")
        self.assertEqual(body["prefetchCount"], 2)


class TrackPositionRouteTests(AppTestCase):
    def test_position_route_propagates_the_allowlisted_sort(self) -> None:
        self.database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
        self.database.upsert_tracks([
            {"chatId": "1", "messageId": "1", "fileName": "z.mp3", "mimeType": "audio/mpeg",
             "title": "Zebra", "sentAt": 300},
            {"chatId": "1", "messageId": "2", "fileName": "a.mp3", "mimeType": "audio/mpeg",
             "title": "apple", "sentAt": 200},
            {"chatId": "1", "messageId": "3", "fileName": "m.mp3", "mimeType": "audio/mpeg",
             "title": "Mango", "sentAt": 100},
        ])

        response = self.client.get("/api/tracks/1:1/position?source=1&sort=title")

        self.assertEqual(200, response.status_code)
        self.assertEqual({"index": 2}, response.json())


class TrackListRouteTests(AppTestCase):
    def test_track_route_exposes_additive_library_metadata(self) -> None:
        self.database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
        self.database.upsert_tracks([
            {"chatId": "1", "messageId": "1", "fileName": "a.mp3", "mimeType": "audio/mpeg", "title": "Needle", "sentAt": 1753835400},
            {"chatId": "1", "messageId": "2", "fileName": "b.mp3", "mimeType": "audio/mpeg", "title": "Other", "sentAt": 1753749000},
        ])

        body = self.client.get("/api/tracks?q=Needle").json()

        self.assertEqual(2, body["allMusicTotal"])
        self.assertIn("dayBreaks", body)
        self.assertEqual(1, body["total"])


class PasswordPolicyTests(AppTestCase):
    SAME_ORIGIN = {"sec-fetch-site": "same-origin"}

    def test_a_short_password_is_rejected(self) -> None:
        response = self.client.post(
            "/api/auth/password", json={"password": "short"}, headers=self.SAME_ORIGIN
        )
        self.assertEqual(response.status_code, 422)
        self.assertFalse(self.database.get_password_hash())

    def test_setting_a_password_locks_the_api(self) -> None:
        response = self.client.post(
            "/api/auth/password",
            json={"password": "long enough password"},
            headers=self.SAME_ORIGIN,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.database.get_password_hash())

    def test_changing_a_password_requires_the_current_one(self) -> None:
        self.database.set_password("the current password")
        self.client.post(
            "/api/auth/login", json={"password": "the current password"}, headers=self.SAME_ORIGIN
        )
        response = self.client.post(
            "/api/auth/password",
            json={"current": "wrong", "password": "a brand new password"},
            headers=self.SAME_ORIGIN,
        )
        self.assertEqual(response.status_code, 401)
        # The change must not have taken effect: the old password still unlocks the app.
        self.client.post("/api/auth/logout", headers=self.SAME_ORIGIN)
        retry = self.client.post(
            "/api/auth/login", json={"password": "the current password"}, headers=self.SAME_ORIGIN
        )
        self.assertEqual(retry.status_code, 200)

    def test_a_correct_current_password_allows_the_change(self) -> None:
        self.database.set_password("the current password")
        self.client.post(
            "/api/auth/login", json={"password": "the current password"}, headers=self.SAME_ORIGIN
        )
        response = self.client.post(
            "/api/auth/password",
            json={"current": "the current password", "password": "a brand new password"},
            headers=self.SAME_ORIGIN,
        )
        self.assertEqual(response.status_code, 200)
        # set_password revokes all sessions, so the handler re-issues one for this caller.
        self.assertEqual(self.client.get("/api/settings").status_code, 200)


class MediaStreamRouteTests(AppTestCase):
    """The byte-serving path through the real route, with a real Media cache on disk.

    TelegramService.start() is stubbed, so the app's MediaCache has no client; every assertion
    here serves from the cache or .part files, which need no Telegram at all.
    """

    SAME_ORIGIN = {"sec-fetch-site": "same-origin"}

    def setUp(self) -> None:
        super().setUp()
        self.database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
        self.database.upsert_tracks([{
            "chatId": "1", "messageId": "2", "fileName": "song.mp3", "mimeType": "audio/mpeg",
            "fileSize": 1000, "title": "Track", "artist": "Artist", "documentId": "9",
        }])
        self.media = self.app.state.telegram.media
        self.identity = media_identity("9", 1000)
        self.digest = media_digest("1:2", self.identity)

    def _seed_cached_file(self) -> None:
        destination = self.media.media_directory / f"{self.digest}.audio"
        destination.write_bytes(b"x" * 1000)
        self.media.database.save_media_cache("1:2", self.identity, destination.name, 1000)

    def test_audio_serves_a_cached_range(self) -> None:
        self._seed_cached_file()
        response = self.client.get("/api/tracks/1:2/audio", headers={"range": "bytes=100-199"})
        self.assertEqual(206, response.status_code)
        self.assertEqual("bytes 100-199/1000", response.headers["content-range"])
        self.assertEqual("100", response.headers["content-length"])
        self.assertEqual(b"x" * 100, response.content)

    def test_audio_serves_the_full_file_without_a_range(self) -> None:
        self._seed_cached_file()
        response = self.client.get("/api/tracks/1:2/audio")
        self.assertEqual(200, response.status_code)
        self.assertEqual("1000", response.headers["content-length"])
        self.assertEqual(b"x" * 1000, response.content)

    def test_audio_out_of_range_is_416_with_the_real_total(self) -> None:
        self._seed_cached_file()
        response = self.client.get("/api/tracks/1:2/audio", headers={"range": "bytes=5000-"})
        self.assertEqual(416, response.status_code)
        self.assertEqual("bytes */1000", response.headers["content-range"])

    def test_audio_head_returns_headers_without_a_body(self) -> None:
        self._seed_cached_file()
        response = self.client.head("/api/tracks/1:2/audio")
        self.assertEqual(200, response.status_code)
        self.assertEqual("1000", response.headers["content-length"])
        self.assertEqual(b"", response.content)

    def test_audio_serves_the_covered_part_of_a_growing_download(self) -> None:
        # A partial file holds 600 of 1000 bytes; the covered range comes from disk with the
        # real total in Content-Range, and an uncovered range must not be served short.
        partial = self.media.media_directory / f"{self.digest}.part"
        partial.write_bytes(b"p" * 600)
        covered = self.client.get("/api/tracks/1:2/audio", headers={"range": "bytes=0-499"})
        self.assertEqual(206, covered.status_code)
        self.assertEqual("bytes 0-499/1000", covered.headers["content-range"])
        self.assertEqual(b"p" * 500, covered.content)
