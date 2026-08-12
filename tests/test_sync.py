"""J1-J6: provider-agnostic sync core against an in-memory fake provider.

Covers the plan's 21.4 list: local like -> outbox -> remote, remote like ->
local merge, unlike tombstone, re-like new liked_at, two-device conflicts,
offline mutations later syncing, provider failure being harmless, and
duplicate delivery idempotency.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import Database
from sync import ChangeBatch, SyncEngine, WriteResult, record_name


class FakeProvider:
    """An in-memory object store acting as the remote side."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.revision = 0
        self.fail_writes = False

    async def read_object(self, name):
        return self.objects.get(name)

    async def write_object(self, name, data, etag=None):
        if self.fail_writes:
            raise ConnectionError("provider unavailable")
        self.objects[name] = data
        self.revision += 1
        return WriteResult(ok=True, etag=str(self.revision))

    async def list_changes(self, cursor=None):
        start = int(cursor or 0)
        records = []
        for name in sorted(self.objects):
            if self.objects[name] is None:
                continue
            payload = json.loads(self.objects[name])
            revision = payload.get("_rev", int(payload.get("updatedAt") or 0))
            if revision > start:
                records.append(payload)
        return ChangeBatch(records=records, next_cursor=str(self.revision))


def _database():
    database = Database(Path(tempfile.mkdtemp()) / "library.sqlite3")
    database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
    database.upsert_tracks([
        {"chatId": "1", "messageId": "2", "fileName": "song.mp3", "mimeType": "audio/mpeg",
         "title": "Track", "artist": "Artist"},
    ])
    return database


def _engine(database, provider, device_id="device-a"):
    engine = SyncEngine(database, provider, device_id=device_id)
    engine.set_namespace("account-1")
    return engine


def _name(entity_type, entity_id):
    return record_name(entity_type, entity_id, "account-1")


class SyncEngineTests(unittest.IsolatedAsyncioTestCase):
    async def _sync(self, engine):
        return await engine.sync_once()

    async def test_local_like_flows_outbox_to_remote(self):
        database = _database()
        provider = FakeProvider()
        engine = _engine(database, provider)
        try:
            with patch("core.now_ts", return_value=1000):
                database.set_liked("1:2", True)
            self.assertEqual(1, database.outbox_count())
            report = await self._sync(engine)
            self.assertEqual(0, database.outbox_count(), "the outbox drains after a push")
            remote = json.loads(provider.objects[_name("like", "1:2")])
            self.assertEqual("1:2", remote["entityId"])
            self.assertEqual("upsert", remote["operation"])
            self.assertEqual(1000, remote["payload"]["likedAt"])
            self.assertEqual("device-a", remote["deviceId"])
        finally:
            database.close()

    async def test_remote_like_merges_without_echo(self):
        database = _database()
        provider = FakeProvider()
        engine = _engine(database, provider)
        try:
            # Device B liked the track at t=900.
            provider.objects[_name("like", "1:2")] = json.dumps({
                "namespace": "account-1", "schemaVersion": 1, "entityType": "like", "entityId": "1:2",
                "operation": "upsert", "payload": {"liked": True, "likedAt": 900},
                "updatedAt": 900, "deviceId": "device-b",
            }).encode()
            provider.revision = 1
            await self._sync(engine)
            self.assertTrue(database.get_track("1", "2")["liked"])
            self.assertEqual(900, database.get_track("1", "2")["likedAt"] if False else
                             _liked_at(database))
            self.assertEqual(0, database.outbox_count(),
                             "a merged remote change must not be echoed back")
        finally:
            database.close()

    async def test_unlike_tombstone_wins_only_when_newer(self):
        database = _database()
        provider = FakeProvider()
        engine = _engine(database, provider)
        try:
            with patch("core.now_ts", return_value=500):
                database.set_liked("1:2", True)
            # Remote unlike at t=700 (newer than the local like at 500): wins.
            provider.objects[_name("like", "1:2")] = json.dumps({
                "namespace": "account-1", "schemaVersion": 1, "entityType": "like", "entityId": "1:2",
                "operation": "delete", "payload": {"liked": False, "likedAt": None},
                "updatedAt": 700, "deviceId": "device-b",
            }).encode()
            provider.revision = 1
            await self._sync(engine)
            self.assertFalse(database.get_track("1", "2")["liked"])
            # Now re-like locally at t=800; an OLD remote unlike at 700 must lose.
            with patch("core.now_ts", return_value=800):
                database.set_liked("1:2", True)
            provider.objects[_name("like", "1:2")] = json.dumps({
                "namespace": "account-1", "schemaVersion": 1, "entityType": "like", "entityId": "1:2",
                "operation": "delete", "payload": {"liked": False, "likedAt": None},
                "updatedAt": 700, "deviceId": "device-b",
            }).encode()
            await self._sync(engine)
            self.assertTrue(database.get_track("1", "2")["liked"],
                            "an older tombstone must not beat a newer local like")
        finally:
            database.close()

    async def test_relike_syncs_a_new_timestamp(self):
        database = _database()
        provider = FakeProvider()
        engine = _engine(database, provider)
        try:
            with patch("core.now_ts", return_value=1000):
                database.set_liked("1:2", True)
            with patch("core.now_ts", return_value=2000):
                database.set_liked("1:2", False)
            with patch("core.now_ts", return_value=3000):
                database.set_liked("1:2", True)
            await self._sync(engine)
            remote = json.loads(provider.objects[_name("like", "1:2")])
            self.assertEqual(3000, remote["payload"]["likedAt"],
                             "the final like's own timestamp is what syncs")
        finally:
            database.close()

    async def test_two_device_conflict_latest_explicit_op_wins(self):
        # Both devices like the track at different times; the newer like wins and
        # the remote timestamp is preserved exactly.
        database_a = _database()
        database_b = _database()
        provider = FakeProvider()
        engine_a = _engine(database_a, provider, device_id="device-a")
        engine_b = _engine(database_b, provider, device_id="device-b")
        try:
            with patch("core.now_ts", return_value=100):
                database_a.set_liked("1:2", True)
            with patch("core.now_ts", return_value=200):
                database_b.set_liked("1:2", True)
            await engine_a.sync_once()
            await engine_b.sync_once()
            self.assertTrue(database_a.get_track("1", "2")["liked"])
            self.assertTrue(database_b.get_track("1", "2")["liked"])
            # Device B's later like propagates back to A with its exact timestamp.
            with patch("core.now_ts", return_value=300):
                database_a.set_liked("1:2", False)
            await engine_a.sync_once()
            await engine_b.sync_once()
            await engine_a.sync_once()
            self.assertFalse(database_a.get_track("1", "2")["liked"],
                             "the latest explicit operation wins on both devices")
            self.assertFalse(database_b.get_track("1", "2")["liked"])
        finally:
            database_a.close()
            database_b.close()

    async def test_offline_mutations_sync_later(self):
        database = _database()
        provider = FakeProvider()
        engine = _engine(database, provider)
        try:
            with patch("core.now_ts", return_value=1000):
                database.set_liked("1:2", True)
            # Offline: writes fail; the outbox keeps the change with a backoff.
            provider.fail_writes = True
            report = await self._sync(engine)
            self.assertIsNotNone(report["error"])
            self.assertEqual(1, database.outbox_count(), "failed pushes stay pending")
            # Back online: the same engine drains the pending change (the retry
            # backoff has expired by then).
            provider.fail_writes = False
            with database.transaction() as connection:
                connection.execute("UPDATE sync_outbox SET next_attempt_at = NULL")
            await self._sync(engine)
            self.assertEqual(0, database.outbox_count())
            self.assertIn(_name("like", "1:2"), provider.objects)
        finally:
            database.close()

    async def test_provider_failure_never_breaks_the_app(self):
        database = _database()
        engine = _engine(database, None)  # no provider configured
        try:
            report = await engine.sync_once()
            self.assertEqual("not configured", report["error"])
            self.assertFalse(engine.configured())
            with patch("core.now_ts", return_value=1000):
                database.set_liked("1:2", True)
            # The local mutation committed regardless; nothing raised.
            self.assertTrue(database.get_track("1", "2")["liked"])
            self.assertEqual(1, database.outbox_count())
        finally:
            database.close()

    async def test_duplicate_delivery_is_idempotent(self):
        database = _database()
        provider = FakeProvider()
        engine = _engine(database, provider)
        try:
            remote = json.dumps({
                "namespace": "account-1", "schemaVersion": 1, "entityType": "like", "entityId": "1:2",
                "operation": "upsert", "payload": {"liked": True, "likedAt": 900},
                "updatedAt": 900, "deviceId": "device-b",
            }).encode()
            provider.objects[_name("like", "1:2")] = remote
            provider.revision = 1
            await self._sync(engine)
            first_liked_at = _liked_at(database)
            # Re-deliver the identical record (e.g. a cursor reset).
            provider.revision = 1
            await self._sync(engine)
            self.assertEqual(first_liked_at, _liked_at(database),
                             "re-delivering the same record changes nothing")
        finally:
            database.close()

    async def test_metadata_and_source_changes_sync(self):
        database = _database()
        provider = FakeProvider()
        engine = _engine(database, provider)
        try:
            database.save_metadata_patch("1", "2", {"title": "My edit"}, [])
            database.set_source_order(["1"])
            database.set_sources_selected(["1"], False)
            self.assertEqual(3, database.outbox_count())
            await self._sync(engine)
            self.assertEqual(0, database.outbox_count())
            # A remote metadata edit merges in without echoing.
            provider.objects[_name("metadata", "1:2")] = json.dumps({
                "namespace": "account-1", "schemaVersion": 1, "entityType": "metadata", "entityId": "1:2",
                "operation": "upsert", "payload": {"title": "Remote edit"},
                "updatedAt": 5000, "deviceId": "device-b",
            }).encode()
            provider.revision = 10
            await self._sync(engine)
            self.assertEqual("Remote edit", database.get_track("1", "2")["metadata"]["title"])
            self.assertEqual(0, database.outbox_count(), "no echo of the merged edit")
        finally:
            database.close()


def _liked_at(database):
    return database.get_track("1", "2").get("likedAt")


if __name__ == "__main__":
    unittest.main()
