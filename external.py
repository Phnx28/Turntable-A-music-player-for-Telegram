from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import httpx

from core import (
    Database,
    lyrics_fingerprint,
    normalize_text,
    parse_lrc,
    plain_lyrics,
    rank_metadata_candidates,
)


ENRICH_MIN_SCORE = 97   # MusicBrainz match score; below this a human decides in the dialog
ENRICH_BATCH = 50       # covers per run; a fresh crate spreads politely over days
ENRICH_GAP = 1.1        # seconds between MusicBrainz calls (the limit is 1/s)


class ExternalServices:
    def __init__(self, database: Database, art_directory: Path, musicbrainz_contact: str):
        self.database = database
        self.art_directory = art_directory
        self.art_directory.mkdir(parents=True, exist_ok=True)
        self.default_musicbrainz_contact = musicbrainz_contact.strip()
        self.http = httpx.AsyncClient(follow_redirects=True, timeout=15, limits=httpx.Limits(max_connections=10, max_keepalive_connections=4))
        self.musicbrainz_lock = asyncio.Lock()
        self.last_musicbrainz_request = 0.0

    async def close(self) -> None:
        await self.http.aclose()

    @property
    def user_agent(self) -> str:
        contact = self.musicbrainz_contact or "configure-MusicBrainz-contact"
        return f"TelegramTurntable/1.0 ({contact})"

    @property
    def musicbrainz_contact(self) -> str:
        return str(
            self.database.get_settings().get("musicbrainzContact")
            or self.default_musicbrainz_contact
        ).strip()

    async def test_musicbrainz(self) -> dict[str, bool]:
        if not self.musicbrainz_contact:
            raise ValueError("Enter a contact email address or website first")
        await self._musicbrainz_get("/recording/", {"query": 'recording:"test"', "fmt": "json", "limit": "1"})
        return {"ok": True}

    async def metadata_candidates(self, track: dict[str, Any], refresh: bool = False) -> list[dict[str, Any]]:
        if not self.musicbrainz_contact:
            raise RuntimeError("Set MUSICBRAINZ_CONTACT before fetching metadata")
        metadata = track["metadata"]
        fingerprint = lyrics_fingerprint(metadata, track["durationMs"])
        cache_key = f"metadata:{track['key']}:{hashlib.sha256(fingerprint.encode()).hexdigest()}"
        if not refresh and (cached := self.database.cache_get(cache_key)) is not None:
            return cached
        title = str(metadata.get("title") or "").strip()
        artist = str(metadata.get("artist") or "").strip()
        if not title:
            raise ValueError("Add a title before fetching metadata")
        query = f'recording:"{self._lucene(title)}"'
        if artist and normalize_text(artist) != "unknown artist":
            query += f' AND artist:"{self._lucene(artist)}"'
        payload = await self._musicbrainz_get(
            "/recording/", {"query": query, "fmt": "json", "limit": "10"}
        )
        candidates = [self._candidate(recording) for recording in payload.get("recordings", [])]
        candidates = [candidate for candidate in candidates if candidate]
        ranked = rank_metadata_candidates(candidates, metadata, track["durationMs"])[:5]
        self.database.cache_set(cache_key, ranked, 24 * 60 * 60)
        return ranked

    async def _musicbrainz_get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        async with self.musicbrainz_lock:
            wait = 1.05 - (time.monotonic() - self.last_musicbrainz_request)
            if wait > 0:
                await asyncio.sleep(wait)
            response = await self.http.get(
                f"https://musicbrainz.org/ws/2{path}",
                params=params,
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            )
            self.last_musicbrainz_request = time.monotonic()
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _lucene(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')[:250]

    @staticmethod
    def _artist_credit(value: Any) -> str:
        if not isinstance(value, list):
            return ""
        return "".join(
            f"{item.get('name', '')}{item.get('joinphrase', '')}"
            for item in value
            if isinstance(item, dict)
        ).strip()

    def _candidate(self, recording: dict[str, Any]) -> dict[str, Any] | None:
        recording_id = recording.get("id")
        if not recording_id:
            return None
        releases = recording.get("releases") or []
        release = next(
            (item for item in releases if item.get("status") == "Official"),
            releases[0] if releases else {},
        )
        release_id = release.get("id", "")
        release_group = release.get("release-group") or {}
        release_group_id = release_group.get("id", "")
        date = release.get("date") or release_group.get("first-release-date") or ""
        media = release.get("media") or []
        medium = media[0] if media else {}
        track_number = 0
        tracks = medium.get("track") or medium.get("tracks") or []
        if tracks:
            try:
                track_number = int(tracks[0].get("position") or 0)
            except (TypeError, ValueError):
                pass
        tags = sorted(
            recording.get("tags") or [], key=lambda item: int(item.get("count") or 0), reverse=True
        )
        cover_url = ""
        if release_group_id:
            cover_url = f"https://coverartarchive.org/release-group/{quote(release_group_id)}/front-500"
        elif release_id:
            cover_url = f"https://coverartarchive.org/release/{quote(release_id)}/front-500"
        candidate_id = f"{recording_id}:{release_id or '-'}"
        try:
            year = int(str(date)[:4]) if date else 0
        except ValueError:
            year = 0
        return {
            "id": candidate_id,
            "recordingId": recording_id,
            "releaseId": release_id,
            "score": int(recording.get("score") or 0),
            "title": recording.get("title") or "",
            "artist": self._artist_credit(recording.get("artist-credit")),
            "album": release.get("title") or "",
            "albumArtist": self._artist_credit(release.get("artist-credit")),
            "genre": tags[0].get("name", "") if tags else "",
            "year": year,
            "trackNumber": track_number,
            "discNumber": int(medium.get("position") or 0),
            "durationMs": int(recording.get("length") or 0),
            "coverUrl": cover_url,
        }

    async def apply_candidate(
        self,
        track: dict[str, Any],
        candidate_id: str,
        fields: Iterable[str] | None = None,
        cover_quality: str = "1200",
    ) -> dict[str, Any]:
        candidates = await self.metadata_candidates(track)
        candidate = next((item for item in candidates if item["id"] == candidate_id), None)
        if not candidate:
            raise KeyError("Metadata candidate expired; search again")
        allowed = {
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
        selected = set(fields or allowed) & allowed
        values = {key: candidate[key] for key in selected if key in candidate and key != "artworkPath"}
        if "artworkPath" in selected and candidate.get("coverUrl"):
            try:
                values["artworkPath"] = await self._download_cover(
                    self._cover_url(candidate["coverUrl"], cover_quality), cover_quality
                )
            except httpx.HTTPStatusError as error:
                if error.response.status_code not in {404, 410}:
                    # 404 (never had a cover) and 410 (removed) are definitive: apply the
                    # candidate's metadata and leave the artwork unset. Anything else
                    # (429/5xx, unexpected 4xx) is transient or surprising: surface it.
                    raise
        return self.database.save_metadata_patch(
            track["chatId"], track["messageId"], values, []
        )

    async def enrich_covers(self, manual: bool = False) -> dict[str, Any]:
        """Automatic cover-art enrichment: policy only, on top of the existing lookups.

        Walks the oldest tracks that still lack artwork and have never been touched by a
        human, and applies Cover Art Archive art when the MusicBrainz match is
        unambiguous (score >= ENRICH_MIN_SCORE). A definitive no-match (404/410, an
        unusable image, or another client error) writes a miss marker so a big crate is
        not re-queried forever; transient failures (408/425/429, 5xx, network errors) do
        NOT write one, so the next run retries. Manual runs ignore the autoArtwork switch
        but still respect the contact requirement.
        """
        settings = self.database.get_settings()
        if not manual and not settings.get("autoArtwork", True):
            return {"added": 0, "missed": 0, "skipped": "disabled"}
        if not self.musicbrainz_contact:
            return {"added": 0, "missed": 0, "skipped": "no-contact"}
        quality = str(settings.get("coverQuality") or "1200")
        added = 0
        missed = 0
        for track in self.database.tracks_needing_artwork(limit=ENRICH_BATCH):
            try:
                candidates = await self.metadata_candidates(track)
                top = candidates[0] if candidates else None
                art_url = ""
                if top and int(top.get("score") or 0) >= ENRICH_MIN_SCORE and top.get("coverUrl"):
                    try:
                        art_url = await self._download_cover(self._cover_url(top["coverUrl"], quality), quality)
                    except ValueError:
                        # Oversized/unsupported image: definitive miss.
                        art_url = ""
                    except httpx.HTTPStatusError as error:
                        if self._is_permanent_cover_miss(error.response):
                            # 404/410 (no such cover) or another client error: definitive miss.
                            art_url = ""
                        else:
                            # 408/425/429 or 5xx: transient. Wait out Retry-After (if any),
                            # then stop the run so the next run retries without a miss marker.
                            await asyncio.sleep(self._retry_after_seconds(error.response))
                            raise
                if not art_url:
                    self.database.mark_artwork_miss(track["key"])
                    missed += 1
                    continue
                self.database.save_metadata_patch(track["chatId"], track["messageId"], {"artworkPath": art_url}, [])
                added += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                # Network blip or MusicBrainz hiccup: stop the run, retry next time, and
                # do not poison the miss markers with a transient failure.
                break
            await asyncio.sleep(ENRICH_GAP)
        return {"added": added, "missed": missed}

    async def candidate_cover(self, track: dict[str, Any], candidate_id: str) -> tuple[bytes, str]:
        candidates = await self.metadata_candidates(track)
        candidate = next((item for item in candidates if item["id"] == candidate_id), None)
        if not candidate or not candidate.get("coverUrl"):
            raise KeyError("Candidate artwork not found")
        url = candidate["coverUrl"]
        if not url.startswith("https://coverartarchive.org/"):
            raise ValueError("Artwork host is not allowed")
        async with self.http.stream("GET", url, headers={"User-Agent": self.user_agent}) as response:
            response.raise_for_status()
            mime_type = response.headers.get("content-type", "").split(";", 1)[0]
            if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
                raise ValueError("Cover Art Archive returned an unsupported image")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 2 * 1024 * 1024:
                    raise ValueError("Artwork preview exceeds 2 MB")
                chunks.append(chunk)
        return b"".join(chunks), mime_type

    @staticmethod
    def _cover_url(url: str, quality: str) -> str:
        if quality not in {"500", "1200", "original"}:
            raise ValueError("Cover quality must be 500, 1200, or original")
        base = url.removesuffix("-500")
        return base if quality == "original" else f"{base}-{quality}"

    @staticmethod
    def _is_permanent_cover_miss(response: httpx.Response) -> bool:
        """True when a CAA error means this track has no usable cover, now or ever.

        404 (not found) and 410 (gone) are definitive. The remaining 4xx codes, apart
        from the explicitly retryable 408/425/429, are client errors: the URL we build
        from MusicBrainz IDs is wrong and retrying it will not help, so they count as
        misses too. 5xx and transport failures are transient and never count.
        """
        status = response.status_code
        return status in {404, 410} or (400 <= status < 500 and status not in {408, 425, 429})

    @staticmethod
    def _retry_after_seconds(response: httpx.Response, cap: float = 60.0) -> float:
        """A bounded Retry-After delay in seconds; 0 when absent or in HTTP-date form."""
        header = response.headers.get("retry-after")
        if not header:
            return 0.0
        try:
            return min(max(float(header), 0.0), cap)
        except ValueError:
            return 0.0

    async def _download_cover(self, url: str, quality: str = "1200") -> str:
        if not url.startswith("https://coverartarchive.org/"):
            raise ValueError("Artwork host is not allowed")
        digest = hashlib.sha256(url.encode()).hexdigest()
        for extension in (".jpg", ".png", ".webp"):
            existing = self.art_directory / f"{digest}{extension}"
            if existing.is_file():
                return existing.name
        temporary = self.art_directory / f".{digest}.tmp"
        total = 0
        mime_type = ""
        buffer = bytearray()
        try:
            async with self.http.stream(
                "GET", url, headers={"User-Agent": self.user_agent}
            ) as response:
                response.raise_for_status()
                mime_type = response.headers.get("content-type", "").split(";", 1)[0]
                if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
                    raise ValueError("Cover Art Archive returned an unsupported image")
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    maximum = 25 if quality == "original" else 12 if quality == "1200" else 5
                    if total > maximum * 1024 * 1024:
                        raise ValueError(f"Artwork exceeds the {maximum} MB limit")
                    buffer.extend(chunk)
            # Artwork is bounded at 25 MB, so buffering it and writing once keeps the
            # event loop free of per-chunk disk I/O.
            await asyncio.to_thread(temporary.write_bytes, bytes(buffer))
            extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[mime_type]
            destination = self.art_directory / f"{digest}{extension}"
            temporary.replace(destination)
            return destination.name
        finally:
            if temporary.exists():
                await asyncio.to_thread(temporary.unlink, missing_ok=True)

    async def lyrics(self, track: dict[str, Any], refresh: bool = False) -> dict[str, Any]:
        metadata = track["metadata"]
        fingerprint = lyrics_fingerprint(metadata, track["durationMs"])
        current = self.database.get_lyrics(track["chatId"], track["messageId"])
        if current and current["kind"] == "manual" and not refresh:
            return current
        if current and not refresh and current["queryFingerprint"] == fingerprint:
            if current["kind"] != "missing" or int(time.time()) - current["fetchedAt"] < 24 * 60 * 60:
                return current
        title = str(metadata.get("title") or "").strip()
        artist = str(metadata.get("artist") or "").strip()
        if not title or not artist or normalize_text(artist) == "unknown artist":
            return self.database.save_lyrics(
                track["chatId"],
                track["messageId"],
                kind="missing",
                plain_text="",
                synced_text="",
                lines=[],
                fingerprint=fingerprint,
            )
        params = {
            "track_name": title,
            "artist_name": artist,
            "duration": str(round(track["durationMs"] / 1000)),
        }
        if metadata.get("album"):
            params["album_name"] = str(metadata["album"])
        response = await self.http.get(
            "https://lrclib.net/api/get",
            params=params,
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
        )
        if response.status_code == 404:
            payload = {"plainLyrics": "", "syncedLyrics": ""}
            kind = "missing"
        else:
            response.raise_for_status()
            payload = response.json()
            kind = "lrclib"
        synced = payload.get("syncedLyrics") or ""
        plain = plain_lyrics(payload.get("plainLyrics") or "")
        lines = parse_lrc(synced, track["durationMs"]) if synced else []
        return self.database.save_lyrics(
            track["chatId"],
            track["messageId"],
            kind=kind,
            plain_text=plain,
            synced_text=synced,
            lines=lines,
            fingerprint=fingerprint,
        )

    def save_manual_lyrics(self, track: dict[str, Any], text: str) -> dict[str, Any]:
        metadata = track["metadata"]
        synced = text if parse_lrc(text, track["durationMs"]) else ""
        lines = parse_lrc(synced, track["durationMs"]) if synced else []
        plain = "\n".join(line["text"] for line in lines) if lines else plain_lyrics(text)
        return self.database.save_lyrics(
            track["chatId"],
            track["messageId"],
            kind="manual",
            plain_text=plain,
            synced_text=synced,
            lines=lines,
            fingerprint=lyrics_fingerprint(metadata, track["durationMs"]),
        )
