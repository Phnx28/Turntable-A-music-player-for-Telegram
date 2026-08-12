"""AcoustID lookup adapter (Phase F3): rate-limited, cached, never blocking.

The lookup turns a Chromaprint fingerprint into candidate recordings (with
MusicBrainz ids, titles, artists and durations). Results are cached in the
app's lookup_cache table keyed by a fingerprint hash + duration bucket, so
re-running enrichment for a track never repeats the API call.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import asdict
from typing import Any

import httpx

from resolver import AcoustIDRecording, ReleaseGroup

ACOUSTID_ENDPOINT = "https://api.acoustid.org/v2/lookup"
ACOUSTID_MIN_INTERVAL_SECONDS = 0.4  # ~2.5 req/s; the free tier allows 3/s per key
ACOUSTID_CACHE_SECONDS = 7 * 24 * 60 * 60
ACOUSTID_TIMEOUT_SECONDS = 20


class AcoustIDError(RuntimeError):
    """Transient lookup failure (network, rate limit, server error)."""


class AcoustIDClient:
    def __init__(
        self,
        database: Any,
        http: httpx.AsyncClient,
        api_key: str,
        user_agent: str,
    ):
        self.database = database
        self.http = http
        self.api_key = (api_key or "").strip()
        self.user_agent = user_agent
        # A process-wide gate keeps lookups polite even when several enrichment
        # workers run at once.
        self._rate_lock = asyncio.Lock()
        self._last_lookup_at = 0.0

    def configured(self) -> bool:
        return bool(self.api_key)

    async def lookup(self, fingerprint: str, duration: float) -> list[AcoustIDRecording]:
        """Candidate recordings for *fingerprint*, cached by fingerprint hash."""
        if not self.configured():
            return []
        if not fingerprint:
            return []
        cache_key = self._cache_key(fingerprint, duration)
        cached = self.database.cache_get(cache_key)
        if cached is not None:
            return [AcoustIDRecording(**entry) for entry in cached]
        payload = await self._request(fingerprint, duration)
        recordings = self._parse(payload)
        # Cache empty results too: a no-match for this fingerprint should not be
        # re-asked of the API on every enrichment pass.
        self.database.cache_set(
            cache_key,
            [asdict(entry) for entry in recordings],
            ACOUSTID_CACHE_SECONDS,
        )
        return recordings

    async def _request(self, fingerprint: str, duration: float) -> dict[str, Any]:
        async with self._rate_lock:
            wait = ACOUSTID_MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_lookup_at)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                response = await self.http.get(
                    ACOUSTID_ENDPOINT,
                    params={
                        "client": self.api_key,
                        "fingerprint": fingerprint,
                        "duration": str(round(duration, 2)),
                        "meta": "recordings+releasegroups+sources",
                    },
                    headers={"User-Agent": self.user_agent},
                    timeout=ACOUSTID_TIMEOUT_SECONDS,
                )
            except (httpx.HTTPError, asyncio.TimeoutError) as error:
                raise AcoustIDError(f"AcoustID lookup failed: {error}") from error
            self._last_lookup_at = time.monotonic()
        if response.status_code == 429:
            raise AcoustIDError("AcoustID rate limit exceeded")
        if response.status_code >= 500:
            raise AcoustIDError(f"AcoustID server error: {response.status_code}")
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _parse(payload: dict[str, Any]) -> list[AcoustIDRecording]:
        recordings: list[AcoustIDRecording] = []
        for result in payload.get("results") or []:
            for raw in result.get("recordings") or []:
                mbid = str(raw.get("id") or "").strip()
                if not mbid:
                    continue
                artist = ", ".join(
                    str(part.get("name") or "")
                    for part in raw.get("artists") or []
                    if part.get("name")
                )
                recordings.append(
                    AcoustIDRecording(
                        mbid=mbid,
                        title=str(raw.get("title") or "").strip(),
                        artist=artist,
                        duration_ms=AcoustIDClient._milliseconds(raw.get("duration")),
                        sources=int(result.get("sources") or 0),
                        release_groups=[
                            ReleaseGroup(
                                id=str(group.get("id") or ""),
                                title=str(group.get("title") or ""),
                                primary_type=str(group.get("type") or ""),
                                secondary_types=[
                                    str(value) for value in group.get("secondarytypes") or []
                                ],
                            )
                            for group in raw.get("releasegroups") or []
                        ],
                    )
                )
        return recordings

    @staticmethod
    def _milliseconds(value: Any) -> int | None:
        try:
            return int(round(float(value) * 1000)) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _cache_key(fingerprint: str, duration: float) -> str:
        # The duration bucket keeps the same fingerprint at very different lengths
        # (a partial file vs the full album track) from sharing a cache entry.
        bucket = round(duration / 5) * 5
        digest = hashlib.sha256(fingerprint.encode()).hexdigest()[:24]
        return f"acoustid:{digest}:{int(bucket)}"
