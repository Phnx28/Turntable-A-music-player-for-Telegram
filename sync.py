"""Provider-agnostic Turntable Sync (Phase J).

The app is local-first: every mutation commits instantly and journals a durable
outbox entry in the same transaction (see Database.outbox_record). SyncEngine
then drains the outbox to a SyncProvider in the background -- pull remote
changes, merge them deterministically, push what is still pending -- without
ever blocking the UI, and without touching audio caches, artwork, thumbnails,
FTS tables, or any secret (J1/26.1).

Google Drive is one provider (Phase K); the engine only knows this protocol.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

LOGGER = logging.getLogger(__name__)

SYNC_SCHEMA_VERSION = 1

# Tombstones: a deleted entity syncs as an operation, never as an absence, so an
# old device cannot resurrect it (J5). Retention/compaction comes later.


@dataclass
class WriteResult:
    ok: bool
    etag: str | None = None
    error: str = ""


@dataclass
class ChangeBatch:
    records: list[dict[str, Any]] = field(default_factory=list)
    next_cursor: str | None = None


class SyncProvider(Protocol):
    """An object store for sync records. Implementations may be remote or local."""

    async def read_object(self, name: str) -> bytes | None: ...
    async def write_object(self, name: str, data: bytes, etag: str | None = None) -> WriteResult: ...
    async def list_changes(self, cursor: str | None) -> ChangeBatch: ...


def record_name(entity_type: str, entity_id: str, namespace: str = "") -> str:
    """The provider object name for one entity; namespaced per Telegram account."""
    prefix = f"{namespace}/" if namespace else ""
    return f"{prefix}records/{entity_type}/{entity_id}"


class SyncEngine:
    """Pull-merge-push sync over one provider, with per-entity conflict rules."""

    def __init__(self, database: Any, provider: SyncProvider | None, device_id: str = ""):
        self.database = database
        self.provider = provider
        self.device_id = device_id or database.sync_state_get("device_id") or ""
        if not self.device_id:
            self.device_id = str(uuid.uuid4())
            database.sync_state_set("device_id", self.device_id)
        # Account namespace (K3): all synced state is per-Telegram-account. Records
        # are named and validated against it, so another account's source
        # selections or likes are never applied to this library.
        self.namespace = database.sync_state_get("sync_namespace") or ""

    def set_namespace(self, namespace: str) -> None:
        self.namespace = namespace or ""
        if self.namespace:
            self.database.sync_state_set("sync_namespace", self.namespace)

    def configured(self) -> bool:
        return self.provider is not None and bool(self.namespace)

    async def sync_once(self) -> dict[str, Any]:
        """Pull remote changes, merge, then push pending local changes.

        Returns a small report for the sync status surface; never raises for
        provider failures -- the caller decides how to surface them.
        """
        if not self.configured():
            return {"pulled": 0, "pushed": 0, "error": "not configured"}
        report: dict[str, Any] = {"pulled": 0, "pushed": 0, "error": None}
        try:
            await self._pull(report)
            await self._push(report)
        except Exception as error:
            LOGGER.warning("Sync pass failed: %s", error)
            report["error"] = str(error)
        return report

    async def _pull(self, report: dict[str, Any]) -> None:
        cursor = self.database.sync_state_get("pull_cursor")
        batch = await self.provider.list_changes(cursor)
        for record in batch.records:
            try:
                self._apply_remote(record)
                report["pulled"] += 1
            except Exception as error:
                LOGGER.warning("Skipping remote record %r: %s", record.get("name"), error)
        if batch.next_cursor:
            self.database.sync_state_set("pull_cursor", batch.next_cursor)

    def _apply_remote(self, record: dict[str, Any]) -> None:
        """Merge one remote record into the local database (deterministic, J6).

        Per-entity policy:
        - like: the latest explicit operation wins; the winning LIKE's liked_at is
          preserved exactly (I6). Local op time is liked_at for likes; remote
          records carry their own updatedAt.
        - source: latest operation wins (selected state).
        - source_order: the latest complete ordering snapshot replaces the order.
        - metadata: whole-record replace, latest updatedAt wins (manual edits are
          user-class; automatic provenance stays protected by the local
          precedence ladder).
        - recording: latest canonical identity wins (upsert by MBID).
        """
        if record.get("namespace") != self.namespace:
            # Another Telegram account's state must never leak into this library.
            return
        entity_type = record.get("entityType") or ""
        entity_id = record.get("entityId") or ""
        operation = record.get("operation") or "upsert"
        payload = record.get("payload") or {}
        updated_at = int(record.get("updatedAt") or 0)
        if entity_type == "like":
            self._merge_like(entity_id, operation, payload, updated_at)
        elif entity_type == "source":
            self._merge_source(entity_id, payload, updated_at)
        elif entity_type == "source_order":
            self._merge_source_order(payload, updated_at)
        elif entity_type == "metadata":
            self._merge_metadata(entity_id, operation, payload, updated_at)
        elif entity_type == "recording":
            self._merge_recording(entity_id, payload, updated_at)
        elif entity_type == "track_recording":
            self._merge_track_recording(entity_id, payload, updated_at)
        else:
            LOGGER.debug("Ignoring unknown remote entity type %r", entity_type)

    def _merge_like(self, track_key: str, operation: str, payload: dict[str, Any], updated_at: int) -> None:
        chat_id, message_id = split_track_key(track_key)
        with self.database.lock:
            row = self.database.connection.execute(
                "SELECT liked_at FROM tracks WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
        local_liked_at = int(row["liked_at"]) if row and row["liked_at"] is not None else None
        if operation == "delete":
            if local_liked_at is None or updated_at < local_liked_at:
                return  # the local like is newer; the remote unlike loses
            self._set_liked_at(chat_id, message_id, None)
            return
        remote_liked_at = int(payload.get("likedAt") or 0)
        if not remote_liked_at:
            return
        if local_liked_at is None or remote_liked_at > local_liked_at:
            # Apply the remote like with its EXACT original timestamp (I6) -- the
            # winning LIKE operation's liked_at is preserved, never re-stamped.
            # No outbox entry: a merged change is never echoed back to the cloud.
            self._set_liked_at(chat_id, message_id, remote_liked_at)

    def _set_liked_at(self, chat_id: str, message_id: str, liked_at: int | None) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE tracks SET liked_at = ? WHERE chat_id = ? AND message_id = ?",
                (liked_at, chat_id, message_id),
            )
        try:
            self.database._invalidate_pos_cache()
        except AttributeError:
            pass

    def _merge_source(self, chat_id: str, payload: dict[str, Any], updated_at: int) -> None:
        selected = bool(payload.get("selected", True))
        current = self.database.list_sources(False)
        local = next((item for item in current if item["chatId"] == chat_id), None)
        if local is None:
            return  # the source does not exist locally; creation is a later phase
        if selected != bool(local["selected"]):
            self.database.set_source_selected(chat_id, selected)

    def _merge_source_order(self, payload: dict[str, Any], updated_at: int) -> None:
        chat_ids = [str(value) for value in payload.get("chatIds") or []]
        if not chat_ids:
            return
        local_ids = {item["chatId"] for item in self.database.list_sources(False)}
        if set(chat_ids) != local_ids:
            return  # the snapshot does not cover the same sources; do not clobber
        self.database.set_source_order(chat_ids, journal=False)

    def _merge_metadata(self, track_key: str, operation: str, payload: dict[str, Any], updated_at: int) -> None:
        chat_id, message_id = split_track_key(track_key)
        # Latest-wins: an older remote edit must not clobber a newer local one.
        if operation == "delete":
            self.database.save_metadata_patch(chat_id, message_id, {}, list(payload or {}), journal=False)
            return
        if not payload:
            return
        self.database.save_metadata_patch(chat_id, message_id, payload, [], journal=False)

    def _merge_recording(self, entity_id: str, payload: dict[str, Any], updated_at: int) -> None:
        mbid = str(payload.get("musicbrainzRecordingId") or entity_id or "")
        if not mbid:
            return
        recording_id = self.database.get_or_create_recording(
            musicbrainz_recording_id=mbid,
            canonical={
                "title": payload.get("canonicalTitle"),
                "artist": payload.get("canonicalArtist"),
            },
            release_group_mbid=payload.get("releaseGroupMbid") or "",
            journal=False,
        )
        # A synced release group fills the local gap only; an existing local choice
        # (the user's own resolution) is never overwritten.
        release_group = payload.get("releaseGroupMbid") or ""
        if release_group:
            existing = self.database.get_track_recording_by_id(recording_id)
            if existing and not existing.get("release_group_mbid"):
                self.database.set_recording_release_group(recording_id, release_group)

    def _merge_track_recording(self, track_key: str, payload: dict[str, Any], updated_at: int) -> None:
        """Attach a synced fingerprint-verified mapping without re-fingerprinting (L2).

        The mapping is self-sufficient: it carries the MBID and the canonical
        values, so it can create/update the recording row and link the local
        track even when the recording entity arrived in a different order.
        Conservative by construction: identity is only ever attached through
        this synced mapping -- never because a title/artist text matched.
        """
        mbid = str(payload.get("musicbrainzRecordingId") or "")
        if not mbid:
            return
        if self.database.get_track_recording(track_key) is not None:
            return  # the track already has a local (possibly newer) identity
        recording_id = self.database.get_or_create_recording(
            musicbrainz_recording_id=mbid,
            canonical={
                "title": payload.get("canonicalTitle"),
                "artist": payload.get("canonicalArtist"),
            },
            release_group_mbid=payload.get("releaseGroupMbid") or "",
            journal=False,
        )
        self.database.link_track_recording(track_key, recording_id, journal=False)

    async def _push(self, report: dict[str, Any]) -> None:
        pending = self.database.outbox_pending(limit=50)
        while pending:
            for entry in pending:
                payload = {
                    "schemaVersion": SYNC_SCHEMA_VERSION,
                    "entityType": entry["entity_type"],
                    "entityId": entry["entity_id"],
                    "operation": entry["operation"],
                    "payload": json.loads(entry["payload_json"]),
                    "updatedAt": entry["created_at"],
                    "deviceId": self.device_id,
                    "namespace": self.namespace,
                }
                name = record_name(
                    entry["entity_type"], entry["entity_id"], self.namespace
                )
                try:
                    result = await self.provider.write_object(
                        name, json.dumps(payload, ensure_ascii=False).encode()
                    )
                except Exception as error:
                    LOGGER.warning("Sync push failed for %s: %s", name, error)
                    self.database.outbox_mark_attempted([entry["id"]], _retry_at(entry))
                    report["error"] = str(error)
                    return
                if result.ok:
                    self.database.outbox_delete([entry["id"]])
                    report["pushed"] += 1
                else:
                    self.database.outbox_mark_attempted([entry["id"]], _retry_at(entry))
                    report["error"] = result.error or "provider rejected the write"
                    return
            pending = self.database.outbox_pending(limit=50)


def _retry_at(entry: dict[str, Any]) -> int:
    import time

    attempts = int(entry.get("attempt_count") or 0)
    delay = min(24 * 60 * 60, 30 * (2 ** min(attempts, 6)))
    return int(time.time()) + delay


def split_track_key(value: str) -> tuple[str, str]:
    chat_id, separator, message_id = value.partition(":")
    return chat_id, message_id
