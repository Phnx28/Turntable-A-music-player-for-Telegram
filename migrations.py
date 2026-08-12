"""The numbered schema migration chain (Phase O1 step 1).

Extracted from Database._migrate: the chain is a pure function of the
connection plus one callback (the FTS rebuild), which is all the migrations
need from the Database. Each block is idempotent by construction -- PRAGMA
user_version gates it -- so re-running is safe.
"""

from __future__ import annotations

from typing import Any, Callable


def apply_migrations(
    connection: Any,
    rebuild_search: Callable[[Any], None],
) -> None:
    """Advance the database schema to the newest version, one numbered step at a time."""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        connection.executescript(
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
        connection.commit()
        version = 1
    if version < 2:
        connection.executescript(
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
                title, artist, album, file_name, tokenize='trigram'
            );

            PRAGMA user_version = 2;
            """
        )
        rebuild_search(connection)
        connection.commit()
        version = 2
    if version < 3:
        connection.executescript(
            """
            CREATE INDEX tracks_available_date
                ON tracks(sent_at DESC) WHERE available = 1;
            PRAGMA user_version = 3;
            """
        )
        connection.commit()
        version = 3
    if version < 4:
        connection.executescript(
            """
            ALTER TABLE tracks ADD COLUMN liked_at INTEGER;
            CREATE INDEX tracks_liked_date
                ON tracks(liked_at DESC, sent_at DESC) WHERE liked_at IS NOT NULL;
            PRAGMA user_version = 4;
            """
        )
        connection.commit()
    if version < 5:
        connection.executescript(
            """
            ALTER TABLE sources ADD COLUMN pinned_at INTEGER;
            PRAGMA user_version = 5;
            """
        )
        connection.commit()
        version = 5
    if version < 6:
        connection.executescript(
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
        connection.commit()
        version = 6
    if version < 7:
        connection.executescript(
            """
            -- FTSv2: rowid-based, no string key column. The old `key UNINDEXED`
            -- + `(chat_id || ':' || message_id) IN (SELECT key ...)` forced a string
            -- concat + bloom-filter per query (7-18ms on 65k). Rowid is an integer
            -- primary key lookup (0.2-1.6ms, 10-40x). Backup is at
            -- backups/library-before-p2-2026-08-08.sqlite3 (WAL-safe .backup).
            DROP TABLE IF EXISTS tracks_fts;
            CREATE VIRTUAL TABLE tracks_fts USING fts5(title, artist, album, file_name, tokenize='trigram');

            PRAGMA user_version = 7;
            """
        )
        rebuild_search(connection)
        connection.commit()
        version = 7
    if version < 8:
        connection.executescript(
            """
            -- Title/artist sort was 25ms+ because the ORDER BY was
            -- COALESCE(NULLIF(json_extract(o.payload...))) which cannot use a btree.
            -- Live DB (65k rows) can't cheaply add a GENERATED column on metadata_overrides,
            -- so index the backing columns instead; the real win comes when sort no longer
            -- depends on the LEFT JOIN. For now, raw telegram_* indexes cover the no-override
            -- fast path (most rows) and shave the common case.
            CREATE INDEX IF NOT EXISTS tracks_title_idx ON tracks(telegram_title COLLATE NOCASE, file_name);
            CREATE INDEX IF NOT EXISTS tracks_artist_idx ON tracks(telegram_artist COLLATE NOCASE, telegram_title COLLATE NOCASE);

            PRAGMA user_version = 8;
            """
        )
        connection.commit()
    if version < 9:
        # Auto cover-art enrichment needs to remember definitive no-matches, or a
        # 65k-track crate would re-query MusicBrainz for the same misses forever.
        # The marker row goes away the moment a manual fetch finds art or the user
        # edits metadata (see clear_artwork_miss callers).
        connection.executescript(
            """
            CREATE TABLE artwork_misses (
                track_key TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL
            );

            PRAGMA user_version = 9;
            """
        )
        connection.commit()
    if version < 10:
        # FTS updates used to be queued in an in-memory set, so a crash between a
        # metadata/title change and the delayed flush could stale the search index
        # forever. Pending keys are now durable: writers add rows to search_dirty
        # in the same transaction as the change, and _flush_search deletes them only
        # after the FTS write commits (reconcile_search() clears leftovers at startup).
        connection.executescript(
            """
            CREATE TABLE search_dirty (
                track_key TEXT PRIMARY KEY,
                updated_at INTEGER NOT NULL
            );

            PRAGMA user_version = 10;
            """
        )
        connection.commit()
    if version < 11:
        # A full source sync used to mark un-seen tracks unavailable with one giant
        # "message_id NOT IN (?, ?, ...)" clause over every seen id -- huge SQL text
        # that approaches SQLite's bind limit on large channels. Tracks now carry
        # the generation of the last completed full scan instead, and
        # complete_sync_generation() flips the un-seen rows with a fixed-shape UPDATE.
        connection.executescript(
            """
            ALTER TABLE tracks ADD COLUMN seen_generation INTEGER;
            CREATE TABLE sync_generations (
                chat_id TEXT PRIMARY KEY,
                generation INTEGER NOT NULL
            );

            PRAGMA user_version = 11;
            """
        )
        connection.commit()
    if version < 12:
        # Recording identity (Phase E1): a Telegram message is *where* a playable copy
        # lives; a recording is *what* it is. Fingerprint-first enrichment resolves
        # tracks to recordings, and likes/metadata will follow the recording, so the
        # identity is stored durably, separate from the location.
        connection.executescript(
            """
            CREATE TABLE recordings (
                id INTEGER PRIMARY KEY,
                musicbrainz_recording_id TEXT UNIQUE,
                acoustid TEXT,
                isrc TEXT,
                canonical_title TEXT,
                canonical_artist TEXT,
                canonical_album TEXT,
                canonical_album_artist TEXT,
                canonical_year INTEGER,
                canonical_track_number INTEGER,
                canonical_disc_number INTEGER,
                release_group_mbid TEXT,
                release_mbid TEXT,
                identity_confidence REAL,
                identity_method TEXT,
                resolver_version INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            ALTER TABLE tracks ADD COLUMN recording_id INTEGER REFERENCES recordings(id);

            PRAGMA user_version = 12;
            """
        )
        connection.commit()
    if version < 13:
        # Per-track automatic-enrichment state (Phase E2): durable status so bulk
        # jobs are resumable and a crash mid-fingerprint never leaves a track stuck
        # "processing" forever (see reset_stale_enrichment_states).
        connection.executescript(
            """
            CREATE TABLE track_enrichment (
                track_key TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                last_attempt_at INTEGER,
                next_retry_at INTEGER,
                failure_code TEXT,
                fingerprint_version INTEGER,
                resolver_version INTEGER
            );

            PRAGMA user_version = 13;
            """
        )
        connection.commit()
    if version < 14:
        # Per-field metadata provenance and locks (Phase E3): user corrections must
        # outrank automatic enrichment forever, so automatic sources may never
        # overwrite a locked field.
        connection.executescript(
            """
            CREATE TABLE metadata_fields (
                recording_id INTEGER NOT NULL,
                field_name TEXT NOT NULL,
                value_json TEXT NOT NULL,
                source TEXT NOT NULL,
                confidence REAL,
                locked INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (recording_id, field_name)
            );

            PRAGMA user_version = 14;
            """
        )
        connection.commit()
    if version < 15:
        # The release-group candidates for a resolved track, held until the
        # release/artwork step settles, so an artwork retry (or a crash between
        # identity link and artwork fetch) never loses the candidates.
        connection.executescript(
            """
            ALTER TABLE track_enrichment ADD COLUMN release_groups_json TEXT;

            PRAGMA user_version = 15;
            """
        )
        connection.commit()
    if version < 16:
        # Provider-agnostic sync (Phase J): local mutations commit instantly and
        # write a durable outbox entry in the same transaction, so the UI never
        # waits on the cloud; the sync engine drains the outbox in the background.
        # sync_state holds the device id, the pull cursor and push bookkeeping.
        connection.executescript(
            """
            CREATE TABLE sync_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at INTEGER
            );

            CREATE INDEX sync_outbox_due ON sync_outbox(next_attempt_at);

            CREATE TABLE sync_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            PRAGMA user_version = 16;
            """
        )
        connection.commit()
