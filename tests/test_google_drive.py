"""K1/K2/K3: the Google Drive provider speaks the SyncProvider protocol.

Drive is faked with httpx.MockTransport: the provider's HTTP surface is the
whole seam, so no live Google calls are needed. Covers per-entity objects in
the appDataFolder, revision cursors via appProperties, name-based read/write,
and token refresh through the manager.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from cryptography.fernet import Fernet

from core import Database
from google_drive import GoogleDriveError, GoogleDriveProvider, GoogleSyncManager
from sync import ChangeBatch, SyncEngine, record_name


class _FakeDrive:
    """A minimal Drive appDataFolder: files keyed by name with a rev property."""

    def __init__(self):
        self.files: dict[str, tuple[str, bytes]] = {}  # name -> (id, content)
        self.counter = 0
        self.revisions: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url.path
        params = request.url.params
        if url.endswith("/files") and request.method == "GET":
            if params.get("spaces") == "appDataFolder":
                files = [
                    {"id": file_id, "name": name, "appProperties": {"rev": str(self.revisions[name])}}
                    for name, (file_id, _) in self.files.items()
                ]
                return httpx.Response(200, json={"files": files})
            return httpx.Response(200, json={"files": []})
        if url.endswith("/files") and request.method == "POST":
            self.counter += 1
            file_id = f"file-{self.counter}"
            name, rev, media = _parse_multipart(request.content)
            self.files[name] = (file_id, media)
            self.revisions[name] = int(rev)
            return httpx.Response(200, json={"id": file_id, "name": name})
        if "/files/" in url and request.method == "PATCH":
            name, rev, media = _parse_multipart(request.content)
            file_id = self.files.get(name, (None, None))[0]
            if file_id is None:
                return httpx.Response(404, json={"error": "not found"})
            self.files[name] = (file_id, media)
            self.revisions[name] = int(rev)
            return httpx.Response(200, json={"id": file_id, "name": name})
        if "/files/" in url and request.method == "GET" and params.get("alt") == "media":
            name = self._name_for(request.url.path.split("/files/")[1])
            for stored_name, (file_id, content) in self.files.items():
                if file_id == request.url.path.split("/files/")[1]:
                    return httpx.Response(200, content=content or b"{}")
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(404, json={"error": "unhandled"})

    @staticmethod
    def _name_for(file_id: str) -> str:
        return file_id


def _parse_multipart(content: bytes) -> tuple[str, int, bytes]:
    text = content.decode("utf-8", "replace")
    name_start = text.find('"name": "')
    if name_start == -1:
        return "", 0, b""
    name = text[name_start + len('"name": "'):].split('"')[0]
    rev_start = text.find('"rev": "')
    rev = int(text[rev_start + len('"rev": "'):].split('"')[0]) if rev_start != -1 else 0
    # The media part is everything after the second boundary header up to the
    # closing boundary.
    marker = b"turntable-sync-boundary"
    parts = content.split(b"--" + marker)
    media = b""
    if len(parts) >= 3:
        media = parts[2]
        media = media.split(b"\r\n\r\n", 1)[-1]
        media = media.rsplit(b"\r\n--", 1)[0]
        if media.endswith(b"\r\n"):
            media = media[:-2]  # the CRLF before the closing boundary is framing
    return name, rev, media


class GoogleDriveProviderTests(unittest.IsolatedAsyncioTestCase):
    def _provider(self, drive):
        transport = httpx.MockTransport(drive.handler)
        http = httpx.AsyncClient(transport=transport)
        provider = GoogleDriveProvider(http, AsyncMock(return_value="test-token"))
        self.addCleanup(lambda: asyncio.get_event_loop().run_until_complete(http.aclose()))
        return provider

    async def test_write_then_read_round_trip(self):
        drive = _FakeDrive()
        provider = self._provider(drive)
        name = record_name("like", "1:2", "account-1")
        payload = json.dumps({"entityType": "like", "entityId": "1:2"}).encode()
        result = await provider.write_object(name, payload)
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.etag)
        self.assertEqual(payload, await provider.read_object(name))
        # A second write updates the same file instead of creating a duplicate.
        self.assertEqual(1, len(drive.files))

    async def test_list_changes_returns_only_newer_records(self):
        drive = _FakeDrive()
        provider = self._provider(drive)
        name = record_name("like", "1:2", "account-1")
        await provider.write_object(name, json.dumps({"entityType": "like", "entityId": "1:2", "rev": 1}).encode())
        first = await provider.list_changes(cursor=None)
        self.assertEqual(1, len(first.records))
        self.assertEqual("1:2", first.records[0]["entityId"])
        # Cursor skips the already-seen record.
        second = await provider.list_changes(cursor=first.next_cursor)
        self.assertEqual(0, len(second.records))

    async def test_missing_object_reads_none(self):
        provider = self._provider(_FakeDrive())
        self.assertIsNone(await provider.read_object(record_name("like", "nope", "account-1")))


class GoogleSyncManagerTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self, drive):
        database = Database(Path(tempfile.mkdtemp()) / "library.sqlite3")
        self.addCleanup(database.close)
        transport = httpx.MockTransport(drive.handler)
        http = httpx.AsyncClient(transport=transport)
        self.addCleanup(lambda: asyncio.get_event_loop().run_until_complete(http.aclose()))
        return GoogleSyncManager(
            database,
            Fernet(Fernet.generate_key()),
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="http://localhost/api/sync/google/callback",
            http=http,
        ), database

    async def test_token_is_encrypted_and_refreshes(self):
        class Drive:
            def __init__(self):
                self.token_calls = 0
                self.refresh_calls = 0

            def handler(self, request):
                if request.url.path.endswith("/token"):
                    body = request.content.decode()
                    if "grant_type=authorization_code" in body:
                        self.token_calls += 1
                        return httpx.Response(200, json={
                            "access_token": "fresh", "refresh_token": "rt", "expires_in": 3600,
                        })
                    self.refresh_calls += 1
                    return httpx.Response(200, json={"access_token": "refreshed", "expires_in": 3600})
                return httpx.Response(404)

        drive = Drive()
        manager, database = self._manager(drive)
        database.sync_state_set("drive_oauth_state", "state")
        email = await manager.complete_login("code", "state")
        self.assertTrue(manager.connected())
        # The stored token is Fernet ciphertext, never plaintext JSON.
        stored = database.sync_state_get("drive_token")
        self.assertNotIn("refresh_token", stored)
        self.assertNotIn("rt", stored)
        token = await manager.get_access_token()
        self.assertEqual("fresh", token)
        # An expired access token refreshes transparently.
        with database.transaction() as connection:
            connection.execute(
                "UPDATE sync_state SET value = ? WHERE key = 'drive_token'",
                (manager.fernet.encrypt(json.dumps({
                    "refresh_token": "rt", "access_token": "stale",
                    "expires_at": 1, "email": "a@b.c",
                }).encode()).decode(),),
            )
        self.assertEqual("refreshed", await manager.get_access_token())
        self.assertEqual(1, drive.refresh_calls)

    async def test_state_mismatch_is_rejected(self):
        manager, _ = self._manager(_FakeDrive())
        with self.assertRaises(GoogleDriveError):
            await manager.complete_login("code", "wrong-state")

    async def test_unconfigured_manager_refuses_to_connect(self):
        database = Database(Path(tempfile.mkdtemp()) / "library.sqlite3")
        self.addCleanup(database.close)
        manager = GoogleSyncManager(
            database, Fernet.generate_key(), "", "", "http://x/callback", httpx.AsyncClient()
        )
        self.assertFalse(manager.configured())


class AccountNamespaceTests(unittest.IsolatedAsyncioTestCase):
    """K3: another account's records are never applied to this library."""

    async def test_foreign_namespace_records_are_ignored(self):
        database = Database(Path(tempfile.mkdtemp()) / "library.sqlite3")
        self.addCleanup(database.close)
        database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
        database.upsert_tracks([
            {"chatId": "1", "messageId": "2", "fileName": "song.mp3", "mimeType": "audio/mpeg",
             "title": "Track", "artist": "Artist"},
        ])
        engine = SyncEngine(database, provider=None, device_id="device-a")
        engine.set_namespace("account-1")
        foreign = {
            "schemaVersion": 1, "namespace": "account-999",
            "entityType": "like", "entityId": "1:2",
            "operation": "upsert", "payload": {"liked": True, "likedAt": 900},
            "updatedAt": 900, "deviceId": "device-b",
        }
        engine._apply_remote(foreign)
        self.assertFalse(database.get_track("1", "2")["liked"],
                         "a different Telegram account's like must not leak in")


if __name__ == "__main__":
    unittest.main()
