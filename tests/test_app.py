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
