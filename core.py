from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import math
import random
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping


METADATA_FIELDS = {
    "title",
    "artist",
    "album",
    "albumArtist",
    "genre",
    "year",
    "trackNumber",
    "discNumber",
    "artworkPath",
}

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav"}

BIND_HOSTS = {"127.0.0.1", "0.0.0.0"}
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60

# scrypt cost: ~16 MB and ~100 ms per attempt, which makes offline cracking of a
# stolen hash expensive while staying imperceptible on a single interactive login.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return "$".join(
        (
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(derived).decode(),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = (encoded or "").split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(hash_b64)
        derived = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, expected)


def session_digest(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def now_ts() -> int:
    return int(time.time())


def track_key(chat_id: str | int, message_id: str | int) -> str:
    return f"{chat_id}:{message_id}"


def split_track_key(value: str) -> tuple[str, str]:
    chat_id, separator, message_id = value.partition(":")
    if not separator or not re.fullmatch(r"-?\d+", chat_id) or not message_id.isdigit():
        raise ValueError("Invalid track key")
    return chat_id, message_id


def normalize_text(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(re.sub(r"[^\w]+", " ", value).split())


def safe_filename(value: str, fallback: str = "track") -> str:
    name = Path(value or "").name
    name = "".join(char for char in name if ord(char) >= 32 and char not in '<>:"/\\|?*')
    name = name.strip(" .")
    if not name:
        name = fallback
    if len(name.encode("utf-8")) > 180:
        suffix = Path(name).suffix[:12]
        stem = Path(name).stem
        while len((stem + suffix).encode("utf-8")) > 180:
            stem = stem[:-1]
        name = stem.rstrip(" .") + suffix
    return name or fallback


def is_audio_file(file_name: str | None, mime_type: str | None) -> bool:
    if (mime_type or "").casefold().startswith("audio/"):
        return True
    return Path(file_name or "").suffix.casefold() in AUDIO_EXTENSIONS


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int
    partial: bool

    @property
    def length(self) -> int:
        return self.end - self.start + 1


class RangeNotSatisfiable(ValueError):
    pass


def parse_range_header(value: str | None, size: int) -> ByteRange:
    if size <= 0:
        raise RangeNotSatisfiable("Unknown or empty file size")
    if value is None:
        return ByteRange(0, size - 1, False)
    if "," in value:
        raise RangeNotSatisfiable("Multiple ranges are not supported")
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match or not any(match.groups()):
        raise RangeNotSatisfiable("Invalid range")
    raw_start, raw_end = match.groups()
    if not raw_start:
        suffix = int(raw_end)
        if suffix <= 0:
            raise RangeNotSatisfiable("Invalid suffix range")
        start = max(0, size - suffix)
        return ByteRange(start, size - 1, True)
    start = int(raw_start)
    if start >= size:
        raise RangeNotSatisfiable("Range begins after the file")
    end = size - 1 if not raw_end else min(int(raw_end), size - 1)
    if end < start:
        raise RangeNotSatisfiable("Range ends before it begins")
    return ByteRange(start, end, True)


_LRC_TIMESTAMP = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_LRC_OFFSET = re.compile(r"^\[offset:([+-]?\d+)\]$", re.IGNORECASE)


def parse_lrc(text: str, duration_ms: int = 0) -> list[dict[str, Any]]:
    parsed: list[tuple[int, str]] = []
    offset = 0
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        offset_match = _LRC_OFFSET.match(line)
        if offset_match:
            offset = int(offset_match.group(1))
            continue
        matches = list(_LRC_TIMESTAMP.finditer(line))
        if not matches:
            continue
        words = _LRC_TIMESTAMP.sub("", line).strip()
        for match in matches:
            fraction = match.group(3) or "0"
            milliseconds = int(fraction.ljust(3, "0")[:3])
            start = (int(match.group(1)) * 60 + int(match.group(2))) * 1000 + milliseconds
            parsed.append((max(0, start + offset), words))
    parsed.sort(key=lambda item: item[0])
    lines: list[dict[str, Any]] = []
    for index, (start, words) in enumerate(parsed):
        next_start = parsed[index + 1][0] if index + 1 < len(parsed) else duration_ms
        end = max(start, next_start or start)
        lines.append({"startMs": start, "endMs": end, "text": words})
    return lines


def plain_lyrics(text: str) -> str:
    return "\n".join(line.rstrip() for line in (text or "").splitlines()).strip()


def lyrics_fingerprint(metadata: Mapping[str, Any], duration_ms: int) -> str:
    return "|".join(
        (
            normalize_text(str(metadata.get("title") or "")),
            normalize_text(str(metadata.get("artist") or "")),
            normalize_text(str(metadata.get("album") or "")),
            str(round(duration_ms / 1000)),
        )
    )


def rank_metadata_candidates(
    candidates: Iterable[dict[str, Any]], metadata: Mapping[str, Any], duration_ms: int
) -> list[dict[str, Any]]:
    wanted_title = normalize_text(str(metadata.get("title") or ""))
    wanted_artist = normalize_text(str(metadata.get("artist") or ""))

    def score(candidate: dict[str, Any]) -> tuple[int, int, int, int]:
        exact_title = normalize_text(candidate.get("title")) == wanted_title
        exact_artist = normalize_text(candidate.get("artist")) == wanted_artist
        candidate_duration = int(candidate.get("durationMs") or 0)
        delta = abs(candidate_duration - duration_ms) if candidate_duration and duration_ms else 999_999
        return (
            int(exact_title),
            int(exact_artist),
            int(delta <= 3_000),
            int(candidate.get("score") or 0) - min(delta // 1_000, 100),
        )

    return sorted(candidates, key=score, reverse=True)


def apply_metadata_patch(
    current: Mapping[str, Any], values: Mapping[str, Any], clear: Iterable[str]
) -> dict[str, Any]:
    result = dict(current)
    unknown = (set(values) | set(clear)) - METADATA_FIELDS
    if unknown:
        raise ValueError(f"Unknown metadata fields: {', '.join(sorted(unknown))}")
    for key in clear:
        result.pop(key, None)
    for key, value in values.items():
        if key in {"year", "trackNumber", "discNumber"}:
            if value in (None, ""):
                result[key] = 0
            elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{key} must be a non-negative integer")
            else:
                result[key] = value
        elif key == "artworkPath":
            if value is not None and not isinstance(value, str):
                raise ValueError("artworkPath must be a string")
            result[key] = value
        else:
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            result[key] = value.strip()[:500]
    return result


def weighted_shuffle_tracks(
    items: Iterable[Mapping[str, Any]], current_key: str = "", random_value: Any = random.random
) -> list[str]:
    now = now_ts()
    values = [dict(item) for item in items if item["key"] != current_key]
    recent = {
        item["key"]
        for item in sorted(values, key=lambda item: int(item.get("lastStartedAt") or 0), reverse=True)[:20]
        if item.get("lastStartedAt")
    } if len(values) > 25 else set()

    def score(item: Mapping[str, Any]) -> float:
        played = int(item.get("playCount") or 0)
        age = now - max(int(item.get("lastStartedAt") or 0), int(item.get("lastPlayedAt") or 0))
        recency = 0.1 if age < 86400 else 0.35 if age < 604800 else 0.7 if age < 2592000 else 1.0
        weight = recency / math.sqrt(1 + played)
        return random_value() ** (1 / max(weight, 0.001))

    head = sorted((item for item in values if item["key"] not in recent), key=score, reverse=True)
    tail = sorted((item for item in values if item["key"] in recent), key=score, reverse=True)
    return [str(item["key"]) for item in (*head, *tail)]


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._track_counts: dict[str, int] | None = None
        # ponytail: defer FTS rebuild until a search-using read needs the index
        self._dirty_search_keys: set[str] = set()
        with self.lock:
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.execute("PRAGMA wal_autocheckpoint = 1000")
            self._migrate()

    def close(self) -> None:
        self._flush_search()
        with self.lock:
            self.connection.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            try:
                yield self.connection
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def _migrate(self) -> None:
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            self.connection.executescript(
                """
                CREATE TABLE telegram_account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    telegram_user_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    encrypted_session BLOB NOT NULL,
                    linked_at INTEGER NOT NULL
                );

                CREATE TABLE sources (
                    chat_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('channel', 'bot', 'private', 'saved')),
                    title TEXT NOT NULL,
                    username TEXT,
                    last_message_id INTEGER NOT NULL DEFAULT 0,
                    last_synced_at INTEGER,
                    sync_error TEXT
                );

                CREATE TABLE tracks (
                    chat_id TEXT NOT NULL REFERENCES sources(chat_id) ON DELETE CASCADE,
                    message_id TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    file_size INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    telegram_title TEXT NOT NULL,
                    telegram_artist TEXT NOT NULL,
                    telegram_album TEXT NOT NULL DEFAULT '',
                    telegram_album_artist TEXT NOT NULL DEFAULT '',
                    telegram_genre TEXT NOT NULL DEFAULT '',
                    telegram_year INTEGER NOT NULL DEFAULT 0,
                    telegram_track_number INTEGER NOT NULL DEFAULT 0,
                    telegram_disc_number INTEGER NOT NULL DEFAULT 0,
                    sent_at INTEGER NOT NULL,
                    available INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (chat_id, message_id)
                );

                CREATE INDEX tracks_source_date ON tracks(chat_id, sent_at DESC);

                CREATE TABLE metadata_overrides (
                    chat_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, message_id),
                    FOREIGN KEY (chat_id, message_id)
                        REFERENCES tracks(chat_id, message_id) ON DELETE CASCADE
                );

                CREATE TABLE lyrics (
                    chat_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('manual', 'lrclib', 'missing')),
                    plain_text TEXT NOT NULL DEFAULT '',
                    synced_text TEXT NOT NULL DEFAULT '',
                    lines_json TEXT NOT NULL DEFAULT '[]',
                    query_fingerprint TEXT NOT NULL DEFAULT '',
                    fetched_at INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, message_id),
                    FOREIGN KEY (chat_id, message_id)
                        REFERENCES tracks(chat_id, message_id) ON DELETE CASCADE
                );

                CREATE TABLE lookup_cache (
                    cache_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );

                PRAGMA user_version = 1;
                """
            )
            self.connection.commit()
            version = 1
        if version < 2:
            self.connection.executescript(
                """
                ALTER TABLE sources ADD COLUMN selected INTEGER NOT NULL DEFAULT 1;
                ALTER TABLE sources ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE sources ADD COLUMN last_post_at INTEGER;
                ALTER TABLE sources ADD COLUMN avatar_version TEXT;
                ALTER TABLE tracks ADD COLUMN document_id TEXT NOT NULL DEFAULT '';

                CREATE TABLE app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE playback_history (
                    chat_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    start_count INTEGER NOT NULL DEFAULT 0,
                    play_count INTEGER NOT NULL DEFAULT 0,
                    skip_count INTEGER NOT NULL DEFAULT 0,
                    last_started_at INTEGER,
                    last_played_at INTEGER,
                    PRIMARY KEY (chat_id, message_id),
                    FOREIGN KEY (chat_id, message_id)
                        REFERENCES tracks(chat_id, message_id) ON DELETE CASCADE
                );

                CREATE TABLE media_cache (
                    track_key TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    last_accessed_at INTEGER NOT NULL
                );

                CREATE VIRTUAL TABLE tracks_fts USING fts5(
                    key UNINDEXED, title, artist, album, file_name, tokenize='trigram'
                );

                PRAGMA user_version = 2;
                """
            )
            self._rebuild_search(self.connection)
            self.connection.commit()
            version = 2
        if version < 3:
            self.connection.executescript(
                """
                CREATE INDEX tracks_available_date
                    ON tracks(sent_at DESC) WHERE available = 1;
                PRAGMA user_version = 3;
                """
            )
            self.connection.commit()
            version = 3
        if version < 4:
            self.connection.executescript(
                """
                ALTER TABLE tracks ADD COLUMN liked_at INTEGER;
                CREATE INDEX tracks_liked_date
                    ON tracks(liked_at DESC, sent_at DESC) WHERE liked_at IS NOT NULL;
                PRAGMA user_version = 4;
                """
            )
            self.connection.commit()
        if version < 5:
            self.connection.executescript(
                """
                ALTER TABLE sources ADD COLUMN pinned_at INTEGER;
                PRAGMA user_version = 5;
                """
            )
            self.connection.commit()
            version = 5
        if version < 6:
            self.connection.executescript(
                """
                CREATE TABLE app_auth (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    password_hash TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE app_sessions (
                    token_hash TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );

                CREATE INDEX app_sessions_expiry ON app_sessions(expires_at);

                PRAGMA user_version = 6;
                """
            )
            self.connection.commit()

    def ping(self) -> bool:
        """Cheap liveness probe so /healthz fails when the DB is locked or gone."""
        try:
            with self.lock:
                self.connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    def get_password_hash(self) -> str:
        with self.lock:
            row = self.connection.execute(
                "SELECT password_hash FROM app_auth WHERE id = 1"
            ).fetchone()
        return str(row["password_hash"]) if row else ""

    def set_password(self, password: str) -> None:
        """Store a new password hash and revoke every existing session."""
        if not password:
            raise ValueError("Password must not be empty")
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO app_auth (id, password_hash, updated_at) VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    password_hash = excluded.password_hash,
                    updated_at = excluded.updated_at
                """,
                (hash_password(password), now_ts()),
            )
            connection.execute("DELETE FROM app_sessions")

    def clear_password(self) -> None:
        with self.transaction() as connection:
            connection.execute("DELETE FROM app_auth")
            connection.execute("DELETE FROM app_sessions")

    def create_session(self, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
        token = secrets.token_urlsafe(32)
        current = now_ts()
        with self.transaction() as connection:
            connection.execute("DELETE FROM app_sessions WHERE expires_at <= ?", (current,))
            connection.execute(
                "INSERT INTO app_sessions (token_hash, created_at, expires_at) VALUES (?, ?, ?)",
                (session_digest(token), current, current + ttl_seconds),
            )
        return token

    def session_valid(self, token: str) -> bool:
        if not token:
            return False
        with self.lock:
            row = self.connection.execute(
                "SELECT expires_at FROM app_sessions WHERE token_hash = ?",
                (session_digest(token),),
            ).fetchone()
        return bool(row) and int(row["expires_at"]) > now_ts()

    def delete_session(self, token: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM app_sessions WHERE token_hash = ?", (session_digest(token),)
            )

    def clear_sessions(self) -> None:
        with self.transaction() as connection:
            connection.execute("DELETE FROM app_sessions")

    def get_account(self) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute("SELECT * FROM telegram_account WHERE id = 1").fetchone()
        return dict(row) if row else None

    def set_account(
        self, telegram_user_id: str, display_name: str, encrypted_session: bytes
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO telegram_account
                    (id, telegram_user_id, display_name, encrypted_session, linked_at)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    telegram_user_id = excluded.telegram_user_id,
                    display_name = excluded.display_name,
                    encrypted_session = excluded.encrypted_session,
                    linked_at = excluded.linked_at
                """,
                (telegram_user_id, display_name, encrypted_session, now_ts()),
            )

    def clear_account(self) -> None:
        with self.transaction() as connection:
            connection.execute("DELETE FROM telegram_account")

    def upsert_source(self, source: Mapping[str, Any], preserve_selection: bool = False) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO sources (chat_id, kind, title, username, selected, last_post_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    kind = excluded.kind,
                    title = excluded.title,
                    username = excluded.username,
                    selected = CASE WHEN ? THEN sources.selected ELSE excluded.selected END,
                    last_post_at = COALESCE(excluded.last_post_at, sources.last_post_at)
                """,
                (
                    str(source["chatId"]),
                    source["kind"],
                    source["title"],
                    source.get("username"),
                    int(source.get("selected", True)),
                    source.get("lastPostAt"),
                    int(preserve_selection),
                ),
            )

    def list_sources(self, selected_only: bool = True) -> list[dict[str, Any]]:
        with self.lock:
            if self._track_counts is None:
                self._track_counts = {
                    str(row["chat_id"]): int(row["track_count"])
                    for row in self.connection.execute(
                        """
                        SELECT chat_id, COUNT(*) AS track_count
                        FROM tracks WHERE available = 1 GROUP BY chat_id
                        """
                    )
                }
            counts = self._track_counts
            rows = self.connection.execute(
                f"""
                SELECT s.*
                FROM sources s
                {"WHERE s.selected = 1" if selected_only else ""}
                ORDER BY s.sort_order, s.title COLLATE NOCASE
                """
            ).fetchall()
        return [
            self._source_row({**dict(row), "track_count": counts.get(str(row["chat_id"]), 0)})
            for row in rows
        ]

    def get_source(self, chat_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM sources WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return self._source_row(row) if row else None

    @staticmethod
    def _source_row(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        value = dict(row)
        return {
            "chatId": str(value["chat_id"]),
            "kind": value["kind"],
            "title": value["title"],
            "username": value.get("username"),
            "selected": bool(value.get("selected", 1)),
            "sortOrder": int(value.get("sort_order", 0)),
            "lastPostAt": value.get("last_post_at"),
            "trackCount": value.get("track_count", 0),
            "lastMessageId": value.get("last_message_id", 0),
            "lastSyncedAt": value.get("last_synced_at"),
            "syncError": value.get("sync_error"),
            "pinnedAt": value.get("pinned_at"),
        }

    def set_source_selected(self, chat_id: str, selected: bool) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE sources SET selected = ? WHERE chat_id = ?", (int(selected), chat_id)
            )
            if not cursor.rowcount:
                raise KeyError("Source not found")

    def set_source_pinned(self, chat_id: str, pinned: bool) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE sources SET pinned_at = ? WHERE chat_id = ?",
                (now_ts() if pinned else None, chat_id),
            )
            if not cursor.rowcount:
                raise KeyError("Source not found")

    def set_sources_selected(self, chat_ids: Iterable[str], selected: bool) -> None:
        self._track_counts = None
        ids = list(dict.fromkeys(str(value) for value in chat_ids))
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self.transaction() as connection:
            connection.execute(
                f"UPDATE sources SET selected = ? WHERE chat_id IN ({placeholders})",
                (int(selected), *ids),
            )

    def set_source_order(self, chat_ids: Iterable[str]) -> None:
        ids = list(dict.fromkeys(str(value) for value in chat_ids))
        selected = {item["chatId"] for item in self.list_sources()}
        if set(ids) != selected:
            raise ValueError("Source order must contain every selected source exactly once")
        with self.transaction() as connection:
            connection.executemany(
                "UPDATE sources SET sort_order = ? WHERE chat_id = ?",
                [(index, chat_id) for index, chat_id in enumerate(ids)],
            )

    def remove_source(self, chat_id: str) -> None:
        self.set_source_selected(chat_id, False)

    def clear_sources(self) -> None:
        self._track_counts = None
        with self.transaction() as connection:
            connection.execute("DELETE FROM sources")

    def finish_sync(self, chat_id: str, last_message_id: int, error: str | None = None) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE sources
                SET last_message_id = MAX(last_message_id, ?),
                    last_synced_at = ?, sync_error = ?
                WHERE chat_id = ?
                """,
                (last_message_id, now_ts(), error, chat_id),
            )

    def upsert_tracks(self, tracks: Iterable[Mapping[str, Any]]) -> None:
        self._track_counts = None
        values = [
            (
                str(item["chatId"]),
                str(item["messageId"]),
                item["fileName"],
                item["mimeType"],
                int(item.get("fileSize") or 0),
                int(item.get("durationMs") or 0),
                item.get("title") or "Unknown title",
                item.get("artist") or "Unknown artist",
                item.get("album") or "",
                item.get("albumArtist") or "",
                item.get("genre") or "",
                int(item.get("year") or 0),
                int(item.get("trackNumber") or 0),
                int(item.get("discNumber") or 0),
                int(item.get("sentAt") or 0),
                str(item.get("documentId") or ""),
            )
            for item in tracks
        ]
        if not values:
            return
        with self.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO tracks (
                    chat_id, message_id, file_name, mime_type, file_size, duration_ms,
                    telegram_title, telegram_artist, telegram_album,
                    telegram_album_artist, telegram_genre, telegram_year,
                    telegram_track_number, telegram_disc_number, sent_at, document_id, available
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET
                    file_name = excluded.file_name,
                    mime_type = excluded.mime_type,
                    file_size = excluded.file_size,
                    duration_ms = excluded.duration_ms,
                    telegram_title = excluded.telegram_title,
                    telegram_artist = excluded.telegram_artist,
                    telegram_album = excluded.telegram_album,
                    telegram_album_artist = excluded.telegram_album_artist,
                    telegram_genre = excluded.telegram_genre,
                    telegram_year = excluded.telegram_year,
                    telegram_track_number = excluded.telegram_track_number,
                    telegram_disc_number = excluded.telegram_disc_number,
                    sent_at = excluded.sent_at,
                    document_id = excluded.document_id,
                    available = 1
                """,
                values,
            )
            for value in values:
                self._dirty_search_keys.add(track_key(value[0], value[1]))

    def mark_missing_unavailable(self, chat_id: str, seen_message_ids: set[str]) -> None:
        self._track_counts = None
        with self.transaction() as connection:
            if seen_message_ids:
                placeholders = ",".join("?" for _ in seen_message_ids)
                connection.execute(
                    f"UPDATE tracks SET available = 0 WHERE chat_id = ? AND message_id NOT IN ({placeholders})",
                    (chat_id, *sorted(seen_message_ids)),
                )
            else:
                connection.execute("UPDATE tracks SET available = 0 WHERE chat_id = ?", (chat_id,))

    def mark_unavailable(self, chat_id: str, message_ids: Iterable[str]) -> None:
        ids = list(message_ids)
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self.transaction() as connection:
            connection.execute(
                f"UPDATE tracks SET available = 0 WHERE chat_id = ? AND message_id IN ({placeholders})",
                (chat_id, *ids),
            )

    def get_track(self, chat_id: str, message_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT t.*, s.title AS source_title, s.kind AS source_kind,
                       s.selected AS source_selected,
                       o.payload AS override_payload
                FROM tracks t
                JOIN sources s ON s.chat_id = t.chat_id
                LEFT JOIN metadata_overrides o
                    ON o.chat_id = t.chat_id AND o.message_id = t.message_id
                WHERE t.chat_id = ? AND t.message_id = ?
                """,
                (chat_id, message_id),
            ).fetchone()
        return self._track_row(row) if row else None

    @staticmethod
    def _search_clause(query: str, parameters: list[Any]) -> str:
        cleaned = query.strip()[:200]
        if not cleaned:
            return ""
        if len(cleaned) >= 3:
            parameters.append(f'"{cleaned.replace(chr(34), chr(34) * 2)}"')
            return (
                "(t.chat_id || ':' || t.message_id) IN "
                "(SELECT key FROM tracks_fts WHERE tracks_fts MATCH ?)"
            )
        escaped = cleaned.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        parameters.extend([f"%{escaped}%"] * 2)
        return (
            "(COALESCE(json_extract(o.payload, '$.title'), t.telegram_title) LIKE ? ESCAPE '\\' "
            "OR COALESCE(json_extract(o.payload, '$.artist'), t.telegram_artist) LIKE ? ESCAPE '\\')"
        )

    @staticmethod
    def _track_summary(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        override = json.loads(value.get("override_payload") or "{}")
        chat_id = str(value["chat_id"])
        message_id = str(value["message_id"])
        artwork = str(override.get("artworkPath") or "")
        fingerprint = artwork or f"{value.get('document_id', '')}:{value.get('file_size', 0)}"
        return {
            "key": track_key(chat_id, message_id),
            "title": override.get("title") or value["telegram_title"] or value["file_name"],
            "artist": override.get("artist") or value["telegram_artist"] or "Unknown artist",
            "durationMs": int(value["duration_ms"] or 0),
            "sentAt": int(value["sent_at"] or 0),
            "artworkVersion": hashlib.sha256(fingerprint.encode()).hexdigest()[:12],
            "liked": value.get("liked_at") is not None,
            "source": {
                "chatId": chat_id,
                "title": value["source_title"],
                "kind": value["source_kind"],
                "selected": bool(value.get("source_selected", 1)),
            },
        }

    def _library_filter(
        self,
        chat_id: str | None = None,
        query: str = "",
        liked: bool = False,
        include_unselected: bool = False,
    ) -> tuple[list[str], list[Any]]:
        if query:
            self._flush_search()
        clauses = ["t.available = 1"]
        parameters: list[Any] = []
        if liked:
            clauses.append("t.liked_at IS NOT NULL")
        elif not include_unselected:
            clauses.append("s.selected = 1")
        if chat_id:
            clauses.append("t.chat_id = ?")
            parameters.append(chat_id)
        if search := self._search_clause(query, parameters):
            clauses.append(search)
        return clauses, parameters

    def list_tracks(
        self,
        chat_id: str | None = None,
        query: str = "",
        offset: int = 0,
        limit: int = 100,
        liked: bool = False,
        include_unselected: bool = False,
        total: int | None = None,
    ) -> dict[str, Any]:
        offset = max(0, int(offset))
        limit = max(25, min(int(limit), 200))
        clauses, parameters = self._library_filter(chat_id, query, liked, include_unselected)
        where = " AND ".join(clauses)
        with self.lock:
            rows = self.connection.execute(
                f"""
                SELECT t.rowid AS track_rowid, t.chat_id, t.message_id, t.file_name,
                       t.file_size, t.duration_ms, t.telegram_title, t.telegram_artist,
                       t.sent_at, t.document_id, s.title AS source_title,
                       s.kind AS source_kind, s.selected AS source_selected,
                       o.payload AS override_payload
                FROM tracks t
                JOIN sources s ON s.chat_id = t.chat_id
                LEFT JOIN metadata_overrides o
                    ON o.chat_id = t.chat_id AND o.message_id = t.message_id
                WHERE {where}
                ORDER BY t.sent_at DESC, t.rowid DESC
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
            # ponytail: COUNT(*) OVER() on this query cost ~200ms on a 55k library because the
            # window function materializes every matching row before the LIMIT. A plain COUNT(*)
            # never builds the rows and lands in single-digit ms, so ask for it separately and
            # only when the caller cannot already know it.
            if total is None:
                count = self.connection.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM tracks t
                    JOIN sources s ON s.chat_id = t.chat_id
                    WHERE {where}
                    """,
                    parameters,
                ).fetchone()[0]
            else:
                count = total
        return {
            "items": [self._track_summary(row) for row in rows],
            "offset": offset,
            "total": int(count),
        }

    def track_position(
        self,
        key: str,
        chat_id: str | None = None,
        query: str = "",
        liked: bool = False,
        include_unselected: bool = False,
    ) -> int:
        target_chat, target_message = split_track_key(key)
        clauses, parameters = self._library_filter(chat_id, query, liked, include_unselected)
        base_clause_text = " AND ".join(clauses) if clauses else "1=1"
        base_params = list(parameters)
        clauses.extend(["t.chat_id = ?", "t.message_id = ?"])
        with self.lock:
            row = self.connection.execute(
                f"""
                SELECT (
                    SELECT COUNT(*) FROM tracks t2
                    JOIN sources s2 ON s2.chat_id = t2.chat_id
                    LEFT JOIN metadata_overrides o2
                      ON o2.chat_id = t2.chat_id AND o2.message_id = t2.message_id
                    WHERE {base_clause_text}
                      AND (t2.sent_at > t.sent_at OR (t2.sent_at = t.sent_at AND t2.rowid > t.rowid))
                ) AS position
                FROM tracks t
                JOIN sources s ON s.chat_id = t.chat_id
                LEFT JOIN metadata_overrides o
                  ON o.chat_id = t.chat_id AND o.message_id = t.message_id
                WHERE {' AND '.join(clauses)}
                """,
                (*base_params, *parameters, target_chat, target_message),
            ).fetchone()
            if not row:
                raise KeyError("Track is not in this playlist")
        return int(row["position"])

    def track_summaries(self, keys: Iterable[str]) -> list[dict[str, Any]]:
        ordered = list(dict.fromkeys(keys))[:100]
        if not ordered:
            return []
        pairs = [split_track_key(key) for key in ordered]
        clauses = "(t.chat_id, t.message_id) IN (" + ", ".join("(?, ?)" for _ in pairs) + ")"
        parameters = [value for pair in pairs for value in pair]
        with self.lock:
            rows = self.connection.execute(
                f"""
                SELECT t.chat_id, t.message_id, t.file_name, t.file_size, t.duration_ms,
                       t.telegram_title, t.telegram_artist, t.sent_at, t.document_id,
                       t.liked_at,
                       s.title AS source_title, s.kind AS source_kind,
                       s.selected AS source_selected, o.payload AS override_payload
                FROM tracks t
                JOIN sources s ON s.chat_id = t.chat_id
                LEFT JOIN metadata_overrides o
                  ON o.chat_id = t.chat_id AND o.message_id = t.message_id
                WHERE t.available = 1 AND ({clauses})
                """,
                parameters,
            ).fetchall()
        found = {track["key"]: track for track in map(self._track_summary, rows)}
        return [found[key] for key in ordered if key in found]

    @staticmethod
    def _track_row(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        source_metadata = {
            "title": value["telegram_title"],
            "artist": value["telegram_artist"],
            "album": value["telegram_album"],
            "albumArtist": value["telegram_album_artist"],
            "genre": value["telegram_genre"],
            "year": value["telegram_year"],
            "trackNumber": value["telegram_track_number"],
            "discNumber": value["telegram_disc_number"],
        }
        overrides = json.loads(value.get("override_payload") or "{}")
        metadata = {**source_metadata, **overrides}
        chat_id = str(value["chat_id"])
        message_id = str(value["message_id"])
        return {
            "key": track_key(chat_id, message_id),
            "chatId": chat_id,
            "messageId": message_id,
            "source": {
                "chatId": chat_id,
                "title": value["source_title"],
                "kind": value["source_kind"],
                "selected": bool(value.get("source_selected", 1)),
            },
            "telegramMetadata": source_metadata,
            "overrides": overrides,
            "metadata": metadata,
            "file": {
                "name": value["file_name"],
                "mimeType": value["mime_type"],
                "size": value["file_size"],
            },
            "durationMs": value["duration_ms"],
            "sentAt": value["sent_at"],
            "available": bool(value["available"]),
            "documentId": value.get("document_id", ""),
            "liked": value.get("liked_at") is not None,
        }

    def set_liked(self, key: str, liked: bool) -> dict[str, Any]:
        chat_id, message_id = split_track_key(key)
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE tracks SET liked_at = ? WHERE chat_id = ? AND message_id = ?",
                (now_ts() if liked else None, chat_id, message_id),
            )
            if not cursor.rowcount:
                raise KeyError("Track not found")
        return self.get_track(chat_id, message_id)  # type: ignore[return-value]

    def liked_count(self) -> int:
        with self.lock:
            return int(self.connection.execute(
                "SELECT COUNT(*) FROM tracks WHERE available = 1 AND liked_at IS NOT NULL"
            ).fetchone()[0])

    def save_metadata_patch(
        self, chat_id: str, message_id: str, values: Mapping[str, Any], clear: Iterable[str]
    ) -> dict[str, Any]:
        track = self.get_track(chat_id, message_id)
        if not track:
            raise KeyError("Track not found")
        updated = apply_metadata_patch(track["overrides"], values, clear)
        with self.transaction() as connection:
            if updated:
                connection.execute(
                    """
                    INSERT INTO metadata_overrides (chat_id, message_id, payload, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(chat_id, message_id) DO UPDATE SET
                        payload = excluded.payload, updated_at = excluded.updated_at
                    """,
                    (chat_id, message_id, json.dumps(updated, ensure_ascii=False), now_ts()),
                )
            else:
                connection.execute(
                    "DELETE FROM metadata_overrides WHERE chat_id = ? AND message_id = ?",
                    (chat_id, message_id),
                )
            self._dirty_search_keys.add(track_key(chat_id, message_id))
        return self.get_track(chat_id, message_id)  # type: ignore[return-value]

    def _flush_search(self) -> None:
        with self.lock:
            if not self._dirty_search_keys:
                return
            keys = list(self._dirty_search_keys)
            self._dirty_search_keys.clear()
        with self.transaction() as connection:
            self._update_search(connection, keys)

    def _rebuild_search(self, connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM tracks_fts")
        rows = connection.execute("SELECT chat_id, message_id FROM tracks").fetchall()
        keys = [track_key(row["chat_id"], row["message_id"]) for row in rows]
        for index in range(0, len(keys), 100):
            self._update_search(connection, keys[index:index + 100], delete=False)

    def _update_search(
        self, connection: sqlite3.Connection, keys: Iterable[str], delete: bool = True
    ) -> None:
        ordered = list(dict.fromkeys(keys))
        if not ordered:
            return
        if len(ordered) > 100:
            for index in range(0, len(ordered), 100):
                self._update_search(connection, ordered[index:index + 100], delete)
            return
        pairs = [split_track_key(key) for key in ordered]
        clauses = "(t.chat_id, t.message_id) IN (" + ", ".join("(?, ?)" for _ in pairs) + ")"
        parameters = [value for pair in pairs for value in pair]
        rows = connection.execute(
            f"""
            SELECT t.chat_id, t.message_id, t.telegram_title, t.telegram_artist,
                   t.telegram_album, t.file_name, o.payload AS override_payload
            FROM tracks t
            LEFT JOIN metadata_overrides o
              ON o.chat_id = t.chat_id AND o.message_id = t.message_id
            WHERE {clauses}
            """,
            parameters,
        ).fetchall()
        if delete:
            placeholders = ",".join("?" for _ in ordered)
            connection.execute(f"DELETE FROM tracks_fts WHERE key IN ({placeholders})", ordered)
        inserts = []
        for row in rows:
            override = json.loads(row["override_payload"] or "{}")
            inserts.append(
                (
                    track_key(row["chat_id"], row["message_id"]),
                    override.get("title", row["telegram_title"]),
                    override.get("artist", row["telegram_artist"]),
                    override.get("album", row["telegram_album"]),
                    row["file_name"],
                )
            )
        connection.executemany(
            "INSERT INTO tracks_fts (key, title, artist, album, file_name) VALUES (?, ?, ?, ?, ?)",
            inserts,
        )

    def get_settings(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "musicbrainzContact": "",
            "coverQuality": "1200",
            "prefetchCount": 1,
            "bindHost": "127.0.0.1",
        }
        with self.lock:
            rows = self.connection.execute("SELECT key, value FROM app_settings").fetchall()
        for row in rows:
            defaults[row["key"]] = json.loads(row["value"])
        return defaults

    def save_settings(self, values: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"musicbrainzContact", "coverQuality", "prefetchCount", "bindHost"}
        if set(values) - allowed:
            raise ValueError("Unknown setting")
        bind_host = values.get("bindHost")
        if bind_host is not None and str(bind_host) not in BIND_HOSTS:
            raise ValueError("Bind address must be 127.0.0.1 or 0.0.0.0")
        contact = values.get("musicbrainzContact")
        if contact is not None and (not isinstance(contact, str) or len(contact.strip()) > 300):
            raise ValueError("MusicBrainz contact must be a short email address or URL")
        quality = values.get("coverQuality")
        if quality is not None and str(quality) not in {"500", "1200", "original"}:
            raise ValueError("Cover quality must be 500, 1200, or original")
        count = values.get("prefetchCount")
        if count is not None and (isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 20):
            raise ValueError("Prefetch count must be between 0 and 20")
        with self.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO app_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                [(key, json.dumps(value)) for key, value in values.items()],
            )
        return self.get_settings()

    def record_playback(self, key: str, event: str) -> None:
        chat_id, message_id = split_track_key(key)
        if event not in {"started", "qualified", "skipped"}:
            raise ValueError("Unknown playback event")
        columns = {
            "started": ("start_count", "last_started_at"),
            "qualified": ("play_count", "last_played_at"),
            "skipped": ("skip_count", "last_started_at"),
        }
        counter, timestamp = columns[event]
        with self.transaction() as connection:
            connection.execute(
                f"""
                INSERT INTO playback_history (chat_id, message_id, {counter}, {timestamp})
                VALUES (?, ?, 1, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET
                    {counter} = {counter} + 1, {timestamp} = excluded.{timestamp}
                """,
                (chat_id, message_id, now_ts()),
            )

    def playback_queue(
        self,
        chat_id: str | None = None,
        query: str = "",
        shuffle: bool = False,
        current_key: str = "",
        liked: bool = False,
        include_unselected: bool = False,
    ) -> list[str]:
        clauses, parameters = self._library_filter(
            chat_id, query, liked, include_unselected
        )
        with self.lock:
            rows = self.connection.execute(
                f"""
                SELECT t.chat_id || ':' || t.message_id AS key,
                       COALESCE(h.play_count, 0) AS play_count,
                       COALESCE(h.last_started_at, 0) AS last_started_at,
                       COALESCE(h.last_played_at, 0) AS last_played_at
                FROM tracks t
                JOIN sources s ON s.chat_id = t.chat_id
                LEFT JOIN metadata_overrides o
                  ON o.chat_id = t.chat_id AND o.message_id = t.message_id
                LEFT JOIN playback_history h
                  ON h.chat_id = t.chat_id AND h.message_id = t.message_id
                WHERE {' AND '.join(clauses)}
                ORDER BY t.sent_at DESC, t.rowid DESC
                """,
                parameters,
            ).fetchall()
        if not shuffle:
            return [str(row["key"]) for row in rows]
        return weighted_shuffle_tracks(
            [
                {
                    "key": row["key"],
                    "playCount": row["play_count"],
                    "lastStartedAt": row["last_started_at"],
                    "lastPlayedAt": row["last_played_at"],
                }
                for row in rows
            ],
            current_key,
        )

    def shuffled_track_keys(self, chat_id: str | None = None, current_key: str = "") -> list[str]:
        return self.playback_queue(chat_id, shuffle=True, current_key=current_key)

    def get_media_cache(self, key: str, fingerprint: str = "") -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM media_cache WHERE track_key = ?", (key,)
            ).fetchone()
        if not row or (fingerprint and row["fingerprint"] != fingerprint):
            return None
        current = now_ts()
        if int(row["last_accessed_at"]) < current - 600:
            with self.transaction() as connection:
                connection.execute(
                    "UPDATE media_cache SET last_accessed_at = ? WHERE track_key = ?",
                    (current, key),
                )
        return dict(row)

    def save_media_cache(self, key: str, fingerprint: str, path: str, size: int) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO media_cache (track_key, fingerprint, path, size, last_accessed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(track_key) DO UPDATE SET fingerprint = excluded.fingerprint,
                    path = excluded.path, size = excluded.size,
                    last_accessed_at = excluded.last_accessed_at
                """,
                (key, fingerprint, path, size, now_ts()),
            )

    def media_cache_entries(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM media_cache ORDER BY last_accessed_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_media_cache(self, keys: Iterable[str] | None = None) -> list[str]:
        values = list(keys or [])
        with self.transaction() as connection:
            if values:
                placeholders = ",".join("?" for _ in values)
                rows = connection.execute(
                    f"SELECT path FROM media_cache WHERE track_key IN ({placeholders})", values
                ).fetchall()
                connection.execute(
                    f"DELETE FROM media_cache WHERE track_key IN ({placeholders})", values
                )
            else:
                rows = connection.execute("SELECT path FROM media_cache").fetchall()
                connection.execute("DELETE FROM media_cache")
        return [row["path"] for row in rows]

    def get_lyrics(self, chat_id: str, message_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM lyrics WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        return {
            "kind": value["kind"],
            "plainText": value["plain_text"],
            "syncedText": value["synced_text"],
            "lines": json.loads(value["lines_json"]),
            "queryFingerprint": value["query_fingerprint"],
            "fetchedAt": value["fetched_at"],
        }

    def save_lyrics(
        self,
        chat_id: str,
        message_id: str,
        *,
        kind: str,
        plain_text: str,
        synced_text: str,
        lines: list[dict[str, Any]],
        fingerprint: str,
    ) -> dict[str, Any]:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO lyrics (
                    chat_id, message_id, kind, plain_text, synced_text,
                    lines_json, query_fingerprint, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET
                    kind = excluded.kind,
                    plain_text = excluded.plain_text,
                    synced_text = excluded.synced_text,
                    lines_json = excluded.lines_json,
                    query_fingerprint = excluded.query_fingerprint,
                    fetched_at = excluded.fetched_at
                """,
                (
                    chat_id,
                    message_id,
                    kind,
                    plain_text,
                    synced_text,
                    json.dumps(lines, ensure_ascii=False),
                    fingerprint,
                    now_ts(),
                ),
            )
        return self.get_lyrics(chat_id, message_id)  # type: ignore[return-value]

    def delete_lyrics(self, chat_id: str, message_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM lyrics WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            )

    def cache_get(self, key: str) -> Any | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT payload, expires_at FROM lookup_cache WHERE cache_key = ?", (key,)
            ).fetchone()
        if not row or row["expires_at"] <= now_ts():
            if row:
                with self.transaction() as connection:
                    connection.execute("DELETE FROM lookup_cache WHERE cache_key = ?", (key,))
            return None
        return json.loads(row["payload"])

    def cache_set(self, key: str, payload: Any, ttl_seconds: int) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO lookup_cache (cache_key, payload, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    payload = excluded.payload, expires_at = excluded.expires_at
                """,
                (key, json.dumps(payload, ensure_ascii=False), now_ts() + ttl_seconds),
            )
