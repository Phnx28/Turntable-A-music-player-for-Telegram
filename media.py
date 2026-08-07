"""Media acquisition and the on-disk Media cache for a Telegram Turntable Track.

The Media cache is the on-disk copy of a Track's audio that replays and seeks are served from.
This module owns the whole byte-sourcing seam: the cached/.part/Telegram triage, the download
and resume protocol, eviction, and the ffmpeg-tagged download. The HTTP layer (app.py) only
maps a MediaSource to a response; it never sees the digest naming, the chunk alignment, or the
.part lifecycle.

The .part on-disk contract is load-bearing and outlives processes: digest-named `.part` files
that are appended, chunk-aligned (offset % MEDIA_CHUNK_SIZE) on resume, renamed to `.audio` on
completion, and swept by TTL. Do not change the naming or alignment scheme; half-downloaded
files depend on it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator

from core import ByteRange, Database, media_digest, media_identity, track_key

LOGGER = logging.getLogger(__name__)

MEDIA_CHUNK_SIZE = 512 * 1024
PARTIAL_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
# A single 512 KiB download chunk that does not arrive within this long means the Telegram
# connection is gone; fail the stream instead of hanging a request forever.
TELEGRAM_CHUNK_TIMEOUT_SECONDS = 120
# Document lookup (a single message fetch) must not stall an audio request indefinitely.
TELEGRAM_MESSAGE_TIMEOUT_SECONDS = 60
# ffmpeg remuxing a track with -c copy is fast; anything over this is a wedged process.
FFMPEG_TIMEOUT_SECONDS = 600
# A .part file untouched for this long is a dead download, not an active one; only those are
# evicted when the cache budget is exceeded.
PARTIAL_EVICTION_GRACE_SECONDS = 5 * 60


class MediaSource:
    """The result of the media triage: total size plus the iterator that serves a byte range.

    Constructed by MediaCache.media_source, which decides where the bytes come from -- a
    complete cache file, the growing .part file (only for ranges it already holds), or a live
    Telegram stream. The HTTP layer reads `size` for headers and calls `iter_range` for the
    body; everything about *where* the bytes live stays behind this interface.
    """

    __slots__ = ("_media", "_track", "size", "_file", "_document")

    def __init__(
        self,
        media: "MediaCache",
        track: dict[str, Any],
        *,
        size: int,
        file: Path | None = None,
        document: Any = None,
    ):
        self._media = media
        self._track = track
        self.size = int(size or 0)
        self._file = file
        self._document = document

    async def iter_range(self, byte_range: ByteRange) -> AsyncIterator[bytes]:
        if self._file is not None:
            try:
                covered = byte_range.start + byte_range.length <= self._file.stat().st_size
            except OSError:
                covered = False  # evicted between construction and open; stream from Telegram
            if covered:
                for chunk in self._media._file_chunks(self._file, byte_range):
                    yield chunk
                return
        document = self._document
        if document is None:
            # The partial file did not cover the range; fetch the document once and stream.
            _, document = await self._media.get_message_document(
                self._track["chatId"], self._track["messageId"]
            )
        async for chunk in self._media.iter_media(document, byte_range.start, byte_range.length):
            yield chunk


class MediaCache:
    def __init__(
        self,
        database: Database,
        *,
        media_directory: Path,
        download_directory: Path,
        client_provider: Any,
        protected_keys: set[str] | None = None,
    ):
        self.database = database
        self.media_directory = media_directory
        self.download_directory = download_directory
        self.media_directory.mkdir(parents=True, exist_ok=True)
        self.download_directory.mkdir(parents=True, exist_ok=True)
        # Called on demand; returns the linked TelegramClient or None. The service passes a
        # provider over its own client, so tests can inject a fake without Telethon.
        self.client_provider = client_provider
        # Tracks the prefetch job is fetching; eviction never touches them. The service owns
        # the set; this module shares it by reference so the protection cannot drift.
        self.protected_keys = protected_keys if protected_keys is not None else set()
        self.cache_lock = asyncio.Lock()
        # ponytail: one global transfer gate is enough for one owner; split by DC only if
        # profiling proves it.
        self.media_semaphore = asyncio.Semaphore(4)
        self.document_cache: dict[str, tuple[float, str, Any, Any]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self.clean_partial_cache()

    def require_client(self) -> Any:
        client = self.client_provider()
        if client is None:
            raise RuntimeError("Link Telegram before using the library")
        return client

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def shutdown(self) -> None:
        for task in list(self._background_tasks):
            task.cancel()
        await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

    def clean_partial_cache(self) -> None:
        cutoff = time.time() - PARTIAL_CACHE_TTL_SECONDS
        for candidate in self.media_directory.glob("*.part"):
            try:
                details = candidate.stat()
                if not details.st_size or details.st_mtime < cutoff:
                    candidate.unlink(missing_ok=True)
            except OSError:
                candidate.unlink(missing_ok=True)

    async def get_message_document(self, chat_id: str, message_id: str) -> tuple[Any, Any]:
        client = self.require_client()
        track = self.database.get_track(chat_id, message_id)
        if not track or not track["available"]:
            raise KeyError("Track is unavailable")
        key = track_key(chat_id, message_id)
        fingerprint = media_identity(track.get("documentId"), track["file"]["size"])
        cached = self.document_cache.get(key)
        if cached and cached[0] > time.monotonic() and cached[1] == fingerprint:
            self.document_cache.pop(key)
            self.document_cache[key] = cached
            return cached[2], cached[3]
        message = await asyncio.wait_for(
            client.get_messages(int(chat_id), ids=int(message_id)),
            timeout=TELEGRAM_MESSAGE_TIMEOUT_SECONDS,
        )
        document = getattr(message, "document", None) if message else None
        if not document:
            self.document_cache.pop(key, None)
            self.database.mark_unavailable(chat_id, [message_id])
            raise KeyError("Telegram media is no longer available")
        self.document_cache[key] = (time.monotonic() + 600, fingerprint, message, document)
        while len(self.document_cache) > 128:
            self.document_cache.pop(next(iter(self.document_cache)))
        return message, document

    async def iter_media(
        self, document: Any, start: int, length: int
    ) -> AsyncIterator[bytes]:
        client = self.require_client()
        request_size = MEDIA_CHUNK_SIZE
        chunks = math.ceil(length / request_size)
        remaining = length
        async with self.media_semaphore:
            iterator = client.iter_download(
                document,
                offset=start,
                limit=chunks,
                chunk_size=request_size,
                request_size=request_size,
                file_size=int(document.size or 0),
            )
            try:
                while remaining > 0:
                    try:
                        chunk = await asyncio.wait_for(
                            iterator.__anext__(),
                            timeout=TELEGRAM_CHUNK_TIMEOUT_SECONDS,
                        )
                    except StopAsyncIteration:
                        break
                    data = bytes(chunk[:remaining])
                    remaining -= len(data)
                    if data:
                        yield data
            except asyncio.TimeoutError as error:
                # The response is already streaming; raising mid-generator just ends the body,
                # which the client's audio retry handles. Do not let the request hang forever.
                raise RuntimeError(
                    "Telegram download timed out mid-stream; check the connection"
                ) from error
            finally:
                close = getattr(iterator, "close", None)
                if close:
                    await close()

    @staticmethod
    def _file_chunks(source_file: Path, byte_range: ByteRange) -> Any:
        remaining = byte_range.length
        with source_file.open("rb") as output:
            output.seek(byte_range.start)
            while remaining:
                chunk = output.read(min(MEDIA_CHUNK_SIZE, remaining))
                if not chunk:
                    # The .part file can be truncated by a resume realignment racing this read
                    # (known, rare, self-healing via the client's audio retry). Stop cleanly
                    # rather than asserting a Content-Length we can no longer honour.
                    break
                remaining -= len(chunk)
                yield chunk

    async def media_source(self, track: dict[str, Any]) -> MediaSource:
        """Triage a Track's media: complete cache, growing .part, or Telegram.

        One document fetch at most on a cold miss -- the double probe the route used to do is
        gone; the fetched document is held by the MediaSource for the stream itself.
        """
        if cached := self.cached_media(track):
            return MediaSource(self, track, size=cached.stat().st_size, file=cached)
        if partial := self.partial_media(track):
            # The total size comes from the database, not Telegram, so a partial download
            # never costs a round trip just to answer the range/416 math.
            return MediaSource(
                self, track, size=int(track["file"]["size"] or 0), file=partial
            )
        _, document = await self.get_message_document(track["chatId"], track["messageId"])
        return MediaSource(
            self,
            track,
            size=int(document.size or track["file"]["size"] or 0),
            document=document,
        )

    def _cache_path(self, name: str) -> Path | None:
        candidate = (self.media_directory / Path(name).name).resolve()
        return candidate if candidate.parent == self.media_directory.resolve() else None

    def cached_media(self, track: dict[str, Any]) -> Path | None:
        fingerprint = (
            media_identity(track.get("documentId"), track["file"]["size"])
            if track.get("documentId")
            else ""
        )
        entry = self.database.get_media_cache(track["key"], fingerprint)
        if not entry or not (candidate := self._cache_path(entry["path"])) or not candidate.is_file():
            return None
        if candidate.stat().st_size != int(track["file"]["size"]):
            self.database.delete_media_cache([track["key"]])
            candidate.unlink(missing_ok=True)
            return None
        return candidate

    def partial_media(self, track: dict[str, Any]) -> Path | None:
        """The growing .part file for *track*, if one exists and holds bytes.

        cache_media writes `{digest}.part` while downloading, so a playing track often has a
        partial file long before it completes. media_source serves the ranges the file already
        covers from disk; only uncached offsets fall back to Telegram streaming.
        """
        document_id = track.get("documentId")
        size = int(track["file"]["size"] or 0)
        if not document_id or size <= 0:
            return None
        identity = media_identity(document_id, size)
        digest = media_digest(track["key"], identity)
        candidate = self.media_directory / f"{digest}.part"
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        except OSError:
            return None
        return None

    async def cache_media(self, track: dict[str, Any]) -> Path:
        if cached := self.cached_media(track):
            return cached
        async with self.cache_lock:
            if cached := self.cached_media(track):
                return cached
            _, document = await self.get_message_document(track["chatId"], track["messageId"])
            expected_size = int(document.size or 0)
            if expected_size <= 0:
                raise RuntimeError("Telegram reported an empty media file")
            fingerprint = media_identity(document.id, expected_size)
            digest = media_digest(track["key"], fingerprint)
            temporary = self.media_directory / f"{digest}.part"
            destination = self.media_directory / f"{digest}.audio"
            stale_entry = self.database.get_media_cache(track["key"])
            if destination.is_file() and destination.stat().st_size == expected_size:
                os.chmod(destination, 0o600)
                if stale_entry and stale_entry["path"] != destination.name:
                    if stale := self._cache_path(stale_entry["path"]):
                        stale.unlink(missing_ok=True)
                self.database.save_media_cache(track["key"], fingerprint, destination.name, expected_size)
                return destination
            destination.unlink(missing_ok=True)
            offset = temporary.stat().st_size if temporary.is_file() else 0
            if offset > expected_size:
                temporary.unlink(missing_ok=True)
                offset = 0
            elif offset < expected_size and offset % MEDIA_CHUNK_SIZE:
                offset -= offset % MEDIA_CHUNK_SIZE
                with temporary.open("r+b") as output:
                    output.truncate(offset)
            if offset < expected_size:
                with temporary.open("ab") as output:
                    os.chmod(temporary, 0o600)
                    async for chunk in self.iter_media(document, offset, expected_size - offset):
                        await asyncio.to_thread(output.write, chunk)
            if temporary.stat().st_size != expected_size:
                raise RuntimeError("Telegram media download ended before the file was complete")
            temporary.replace(destination)
            os.chmod(destination, 0o600)
            if stale_entry and stale_entry["path"] != destination.name:
                if stale := self._cache_path(stale_entry["path"]):
                    stale.unlink(missing_ok=True)
            self.database.save_media_cache(
                track["key"], fingerprint, destination.name, destination.stat().st_size
            )
            await self._evict_cache()
            return destination

    def start_cache_current(self, key: str) -> dict[str, Any]:
        """Background-cache the playing track without touching the prefetch job."""
        if key:
            self._spawn(self._cache_current(key))
        return {"ok": True}

    async def _cache_current(self, key: str) -> None:
        try:
            chat_id, message_id = key.split(":", 1)
            track = await asyncio.to_thread(self.database.get_track, chat_id, message_id)
            if not track or not track["available"]:
                return
            await self.cache_media(track)
        except Exception:
            # Streaming still works without the cache; never let a failed background cache
            # surface as an error dialog.
            LOGGER.exception("Could not cache the current track %s", key)

    async def tagged_download(self, track: dict[str, Any]) -> Path | None:
        if not track.get("overrides"):
            return None
        source = await self.cache_media(track)
        metadata = track["metadata"]
        fingerprint = json.dumps(
            [track["key"], track.get("documentId"), track["file"]["size"], track["overrides"]],
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(fingerprint.encode()).hexdigest()
        suffix = Path(track["file"]["name"]).suffix or ".mp3"
        destination = self.download_directory / f"{digest}{suffix}"
        if destination.is_file():
            return destination
        temporary = self.download_directory / f"{digest}.part{suffix}"
        fields = {
            "title": metadata.get("title"),
            "artist": metadata.get("artist"),
            "album": metadata.get("album"),
            "album_artist": metadata.get("albumArtist"),
            "genre": metadata.get("genre"),
            "date": metadata.get("year"),
            "track": metadata.get("trackNumber"),
            "disc": metadata.get("discNumber"),
        }
        command = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(source), "-map", "0", "-c", "copy"]
        for name, value in fields.items():
            if value not in (None, "", 0):
                command.extend(["-metadata", f"{name}={value}"])
        command.append(str(temporary))
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
        except OSError as error:
            # Almost always ffmpeg missing from PATH. The caller falls back to the untagged
            # original, which is the right behaviour but indistinguishable from success unless it
            # is said out loud -- edits appear to be silently discarded.
            LOGGER.warning(
                "Cannot tag %s for download: ffmpeg could not be run (%s). "
                "Serving the original file, so edited metadata will be missing. "
                "Install ffmpeg to include it.",
                track["key"],
                error,
            )
            return None
        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=FFMPEG_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            temporary.unlink(missing_ok=True)
            LOGGER.warning("Cannot tag %s for download: ffmpeg timed out", track["key"])
            return None
        if process.returncode:
            temporary.unlink(missing_ok=True)
            LOGGER.warning(
                "Cannot tag %s for download: ffmpeg exited %s. %s",
                track["key"],
                process.returncode,
                stderr.decode("utf-8", "replace").strip() or "No error output.",
            )
            return None
        temporary.replace(destination)
        os.chmod(destination, 0o600)
        return destination

    async def _evict_cache(self, maximum: int = 5 * 1024 * 1024 * 1024) -> None:
        await asyncio.to_thread(self._evict_cache_sync, maximum)

    def _evict_cache_sync(self, maximum: int = 5 * 1024 * 1024 * 1024) -> None:
        entries = self.database.media_cache_entries()
        total = sum(int(entry["size"]) for entry in entries)
        now = time.time()
        partials: list[tuple[float, int, Path]] = []
        for candidate in self.media_directory.glob("*.part"):
            try:
                details = candidate.stat()
            except OSError:
                candidate.unlink(missing_ok=True)
                continue
            total += details.st_size
            partials.append((details.st_mtime, details.st_size, candidate))
        # Oldest dead downloads first; the completion order of cache entries is less relevant
        # than never interrupting a download that is actively writing.
        partials.sort()
        remove: list[str] = []
        for entry in entries:
            if total <= maximum:
                break
            if entry["track_key"] in self.protected_keys:
                continue
            total -= int(entry["size"])
            remove.append(entry["track_key"])
            if candidate := self._cache_path(entry["path"]):
                candidate.unlink(missing_ok=True)
        for mtime, size, candidate in partials:
            if total <= maximum:
                break
            if now - mtime < PARTIAL_EVICTION_GRACE_SECONDS:
                continue
            total -= size
            candidate.unlink(missing_ok=True)
        if remove:
            self.database.delete_media_cache(remove)

    def clear_cache(self) -> dict[str, int]:
        """Delete every cached file and .part download, and their database rows."""
        paths = self.database.delete_media_cache()
        removed = 0
        for name in paths:
            if candidate := self._cache_path(name):
                if candidate.exists():
                    removed += candidate.stat().st_size
                    candidate.unlink()
        for candidate in self.media_directory.glob("*.part"):
            try:
                size = candidate.stat().st_size
                candidate.unlink()
                removed += size
            except OSError:
                candidate.unlink(missing_ok=True)
        return {"removedBytes": removed}
