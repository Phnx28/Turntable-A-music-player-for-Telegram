"""Silent, fingerprint-first metadata enrichment (Phases F, and later H).

The single entry point is enrich_track(): playback-triggered (F7) and source
bulk (H) enrichment both call it, so there is exactly one resolver pipeline.

Pipeline: local audio -> Chromaprint -> AcoustID -> recording resolver ->
durable identity + provenance fields. Nothing here blocks playback: the
trigger fires after playback has started, the pipeline runs in background
jobs, and every failure lands in the durable enrichment state (Phase E2) as a
retryable temporary failure rather than a permanent miss.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Literal

from acoustid import AcoustIDClient, AcoustIDError
from fingerprints import FingerprintError, FingerprintService
from resolver import (
    ReleaseGroup,
    ResolutionDecision,
    ResolutionKind,
    decide,
    resolve_release_group,
)

LOGGER = logging.getLogger(__name__)

RESOLVER_VERSION = 1

# Temporary failures retry with exponential backoff, bounded.
RETRY_BASE_SECONDS = 30
RETRY_MAX_SECONDS = 24 * 60 * 60

# Bulk enrichment concurrency (Phase H5): conservative, internal constants.
BULK_TRACK_CONCURRENCY = 2
BULK_COVER_CONCURRENCY = 3
BULK_BATCH_SIZE = 25
BULK_CLUSTER_WINDOW = 20  # resolved neighbours whose release groups vote (H7)

# Source label for provenance rows written by this pipeline.
_ENRICHMENT_SOURCE = "fingerprint_resolver"

# Statuses that mean "never re-attempt this track automatically".
_TERMINAL = {"resolved", "ambiguous", "no_match", "manual_override"}

# A no_match whose failure code names a *missing capability* (no fpcalc binary, no
# AcoustID key) is not a definitive negative: the track was never actually judged.
# Once the capability exists, playback and bulk enrichment retry it instead of
# treating it as final, so installing fpcalc later is not silently ignored.
_RETRYABLE_NO_MATCH_CODES = {"fingerprint-unavailable", "acoustid-unconfigured"}


class EnrichmentResult:
    """Structured outcome of one enrich_track call."""

    __slots__ = ("decision", "recording_id", "reason")

    def __init__(self, decision: str, recording_id: int | None = None, reason: str = ""):
        self.decision = decision
        self.recording_id = recording_id
        self.reason = reason


class EnrichmentService:
    """The app-level facade wiring the resolver pipeline into the running services."""

    def __init__(
        self,
        database: Any,
        media: Any,
        fingerprints: FingerprintService,
        acoustid: AcoustIDClient,
        cover_fetcher: Callable[[str, str], Any] | None = None,
    ):
        self.database = database
        self.media = media
        self.fingerprints = fingerprints
        self.acoustid = acoustid
        # cover_fetcher(release_group_id, quality) -> artwork file name, or None on a
        # definitive miss; transient failures raise (see external.fetch_release_group_cover).
        self.cover_fetcher = cover_fetcher
        # Playback-triggered runs in flight, keyed by track key, so rapid play events
        # never start a second fingerprint for the same track.
        self._in_flight: set[str] = set()
        # Bulk concurrency (Phase H5): conservative, internal constants. Playback
        # triggers bypass these gates entirely, so bulk enrichment yields to playback.
        self.bulk_semaphore = asyncio.Semaphore(BULK_TRACK_CONCURRENCY)
        self.cover_semaphore = asyncio.Semaphore(BULK_COVER_CONCURRENCY)

    async def enrich_track(
        self,
        track: dict[str, Any],
        *,
        trigger: Literal["playback", "bulk", "manual"],
        replace_existing: bool = False,
        context_hint: str = "",
    ) -> EnrichmentResult:
        key = track["key"]
        if key in self._in_flight:
            return EnrichmentResult("skipped", reason="already in flight")
        self._in_flight.add(key)
        try:
            # Bulk runs share the concurrency gates; playback/manual runs never wait
            # behind them (Phase H6: playback always wins over bulk enrichment).
            if trigger == "bulk":
                async with self.bulk_semaphore:
                    return await _enrich_track(
                        self.database,
                        self.media,
                        self.fingerprints,
                        self.acoustid,
                        track,
                        trigger=trigger,
                        replace_existing=replace_existing,
                        cover_fetcher=self.cover_fetcher,
                        context_hint=context_hint,
                        cover_gate=self.cover_semaphore,
                    )
            return await _enrich_track(
                self.database,
                self.media,
                self.fingerprints,
                self.acoustid,
                track,
                trigger=trigger,
                replace_existing=replace_existing,
                cover_fetcher=self.cover_fetcher,
                context_hint=context_hint,
                cover_gate=self.cover_semaphore,
            )
        finally:
            self._in_flight.discard(key)

    async def bulk_enrich_source(
        self,
        job: Any,
        chat_id: str,
        scope: str,
        *,
        fetch_artwork: bool,
        replace_existing: bool,
    ) -> None:
        """The resumable bulk job body (Phase H): one track at a time, durable state.

        Walks the source's eligible tracks in rowid order, skipping anything the
        pipeline settles (each track's enrichment state commits as it goes, so an
        interrupted job resumes where the states say). Playback-triggered
        enrichment and the cache-current task always outrank this job.
        """
        if scope == "reprocess":
            # Reprocessing everything means re-running the pipeline for the source;
            # identity links are kept, so already-resolved tracks re-check artwork.
            self.database.reset_source_enrichment_states(chat_id)
        last_rowid = 0
        recent_groups: list[str] = []
        while True:
            batch = self.database.tracks_for_enrichment(
                chat_id, scope, limit=BULK_BATCH_SIZE, after_rowid=last_rowid
            )
            if not batch:
                break
            hint = _most_common(recent_groups[-BULK_CLUSTER_WINDOW:])
            results = await asyncio.gather(
                *[
                    self.enrich_track(
                        track,
                        trigger="bulk",
                        replace_existing=replace_existing,
                        context_hint=hint,
                    )
                    for track in batch
                ],
                return_exceptions=True,
            )
            for track, result in zip(batch, results):
                job.processed += 1
                if isinstance(result, Exception):
                    LOGGER.warning("Bulk enrichment failed for %s: %s", track["key"], result)
                elif result.decision in {"resolved", "auto_apply", "fill_missing_only"}:
                    job.found += 1
                recording = self.database.get_track_recording(track["key"])
                if recording and recording.get("release_group_mbid"):
                    recent_groups.append(str(recording["release_group_mbid"]))
            last_rowid = int(batch[-1].get("rowid") or 0)
            job.result = {"lastRowid": last_rowid}
            # Give the event loop (and playback) room between batches.
            await asyncio.sleep(0)

    def enrich_playback(self, track: dict[str, Any]) -> None:
        """Silent playback-triggered enrichment (Phase F7): fire and forget.

        Never blocks playback, never shows UI; the durable enrichment state
        dedupes against recent attempts.
        """
        key = track["key"]
        state = self.database.get_enrichment_state(key)
        if state and state["status"] in _TERMINAL:
            return
        if state and state["status"] == "temporary_failure":
            retry_at = state.get("next_retry_at") or 0
            if retry_at and time.time() < retry_at:
                return
        asyncio.create_task(self.enrich_track(track, trigger="playback"))


async def _enrich_track(
    database: Any,
    media: Any,
    fingerprints: FingerprintService,
    acoustid: AcoustIDClient,
    track: dict[str, Any],
    *,
    trigger: Literal["playback", "bulk", "manual"],
    replace_existing: bool,
    cover_fetcher: Callable[[str, str], Any] | None,
    context_hint: str = "",
    cover_gate: Any = None,
) -> EnrichmentResult:
    """Resolve *track* to a recording identity and persist it.

    *trigger* is recorded for policy/debugging; *replace_existing* lets the
    bulk UI ask for fingerprint-verified replacement of Telegram metadata
    (Phase H), while playback enrichment never replaces.
    """
    key = track["key"]
    state = database.get_enrichment_state(key)
    if state and state["status"] in _TERMINAL:
        if not (state["status"] == "no_match" and state.get("failure_code") in _RETRYABLE_NO_MATCH_CODES):
            return EnrichmentResult("skipped", reason=f"terminal state: {state['status']}")
    if state and state["status"] == "fingerprinting":
        return EnrichmentResult("skipped", reason="already processing")
    if state and state["status"] == "temporary_failure":
        retry_at = state.get("next_retry_at") or 0
        if retry_at and time.time() < retry_at:
            return EnrichmentResult("skipped", reason="within retry backoff")

    if not acoustid.configured():
        database.set_enrichment_state(
            key, "no_match", failure_code="acoustid-unconfigured", resolver_version=RESOLVER_VERSION
        )
        return EnrichmentResult("no_match", reason="AcoustID is not configured")

    # A linked recording means the identity half already settled (e.g. the artwork
    # step failed transiently last run); re-enter straight at release/artwork.
    recording = database.get_track_recording(key)
    if recording is not None:
        return await _release_and_artwork(
            database, track, recording, cover_fetcher,
            context_hint=context_hint, cover_gate=cover_gate,
        )

    database.set_enrichment_state(
        key, "fingerprinting", resolver_version=RESOLVER_VERSION
    )
    try:
        source = await media.fingerprint_source(track)
        if source is None:
            return _temporary(database, key, "no local audio for fingerprinting")
        try:
            fingerprint = await fingerprints.fingerprint_track(source)
        except FingerprintError as error:
            LOGGER.warning("Fingerprint failed for %s: %s", key, error)
            database.set_enrichment_state(
                key, "no_match", failure_code="fingerprint-unavailable",
                resolver_version=RESOLVER_VERSION,
            )
            return EnrichmentResult("no_match", reason=str(error))
        try:
            candidates = await acoustid.lookup(fingerprint.fingerprint, fingerprint.duration)
        except AcoustIDError as error:
            LOGGER.warning("AcoustID lookup failed for %s: %s", key, error)
            return _temporary(database, key, str(error))
        decision = decide(
            telegram_title=track["metadata"].get("title"),
            telegram_artist=track["metadata"].get("artist"),
            audio_duration_s=fingerprint.duration,
            candidates=candidates,
        )
        if decision.kind in {ResolutionKind.AMBIGUOUS, ResolutionKind.NO_MATCH}:
            database.set_enrichment_state(
                key, str(decision.kind.value), resolver_version=RESOLVER_VERSION
            )
            return EnrichmentResult(str(decision.kind.value), reason=decision.reason)
        assert decision.recording is not None
        recording_id = _apply_identity(database, track, decision, replace_existing=replace_existing)
        database.set_enrichment_state(
            key,
            "fingerprinting",
            resolver_version=RESOLVER_VERSION,
            release_groups_json=json.dumps(
                [
                    {
                        "id": group.id,
                        "title": group.title,
                        "primary_type": group.primary_type,
                        "secondary_types": group.secondary_types,
                    }
                    for group in decision.recording.release_groups
                ],
                ensure_ascii=False,
            ),
        )
        recording = database.get_track_recording(key)
        return await _release_and_artwork(
            database, track, recording, cover_fetcher,
            context_hint=context_hint, cover_gate=cover_gate,
        )
    except asyncio.CancelledError:
        # The state stays "fingerprinting"; startup recovery flips it to retryable.
        raise
    except Exception:
        LOGGER.exception("Enrichment pipeline failed for %s", key)
        return _temporary(database, key, "pipeline failure")


def _apply_identity(
    database: Any,
    track: dict[str, Any],
    decision: ResolutionDecision,
    *,
    replace_existing: bool,
) -> int:
    """Link the recording identity and write provenance fields (no state change)."""
    recording = decision.recording
    assert recording is not None
    recording_id = database.get_or_create_recording(
        musicbrainz_recording_id=recording.mbid,
        acoustid="",  # the lookup returns recordings, not the fingerprint's AcoustID
        canonical={
            "title": recording.title,
            "artist": recording.artist,
        },
        confidence=0.98 if decision.kind == ResolutionKind.AUTO_APPLY else 0.8,
        method="acoustid",
        resolver_version=RESOLVER_VERSION,
    )
    database.link_track_recording(track["key"], recording_id)
    # Provenance rows (title/artist come from the recording; album/year/numbers
    # arrive with the release/artwork step). set_metadata_field itself refuses
    # to overwrite locked or higher-precedence values, and the display layering
    # keeps user overrides on top of these automatic values.
    current = track["metadata"]
    for field, value in (("title", recording.title), ("artist", recording.artist)):
        if not str(value or "").strip():
            continue
        # FILL_MISSING_ONLY never touches a field the track already displays;
        # AUTO_APPLY may (the fingerprint gate passed, and the display layering
        # still lets a user correction outrank it). Bulk enrichment may force a
        # fill of existing fields with fingerprint-verified values.
        if decision.kind == ResolutionKind.FILL_MISSING_ONLY and not replace_existing \
                and _has_value(current.get(field)):
            continue
        database.set_metadata_field(
            recording_id,
            field,
            value,
            source=_ENRICHMENT_SOURCE,
            confidence=0.98 if decision.kind == ResolutionKind.AUTO_APPLY else 0.8,
        )
    return recording_id


async def _release_and_artwork(
    database: Any,
    track: dict[str, Any],
    recording: dict[str, Any] | None,
    cover_fetcher: Callable[[str, str], Any] | None,
    *,
    context_hint: str = "",
    cover_gate: Any = None,
) -> EnrichmentResult:
    """Resolve the release group and fetch its cover for a resolved recording.

    Settles the enrichment state: resolved (artwork in place or definitively
    missing), or temporary_failure (artwork step hit a transient error and the
    next run re-enters here, skipping the fingerprint).
    """
    key = track["key"]
    recording_id = int(recording["id"])
    release_group_id = recording.get("release_group_mbid") or ""
    if not release_group_id:
        state = database.get_enrichment_state(key)
        groups = _release_groups_from_state(state)
        chosen = resolve_release_group(
            groups,
            album_tag=track["metadata"].get("album"),
            year=_int_or_none(track["metadata"].get("year")),
            track_number=_int_or_none(track["metadata"].get("trackNumber")),
            hint=context_hint or None,
        )
        if chosen is None:
            # No plausible release group: the recording stays resolved, and artwork
            # falls back to the existing text-search policy (G3 fallback order).
            database.set_enrichment_state(
                key, "resolved", failure_code="no-release-group",
                resolver_version=RESOLVER_VERSION,
            )
            return EnrichmentResult("resolved", recording_id=recording_id,
                                    reason="resolved without a release group")
        release_group_id = chosen.id
        database.set_recording_release_group(recording_id, release_group_id)
    if cover_fetcher is None:
        database.set_enrichment_state(
            key, "resolved", resolver_version=RESOLVER_VERSION,
        )
        return EnrichmentResult("resolved", recording_id=recording_id)
    quality = str(database.get_settings().get("coverQuality") or "1200")
    try:
        if cover_gate is not None:
            async with cover_gate:
                artwork = await cover_fetcher(release_group_id, quality)
        else:
            artwork = await cover_fetcher(release_group_id, quality)
    except Exception as error:
        LOGGER.warning("Release-group cover failed transiently for %s: %s", key, error)
        return _temporary(database, key, "artwork retry")
    if artwork is None:
        # Definitive miss (404/410): long-lived marker, artwork settled.
        database.mark_artwork_miss(key)
        database.set_enrichment_state(
            key, "resolved", failure_code="artwork-missing",
            resolver_version=RESOLVER_VERSION,
        )
        return EnrichmentResult("resolved", recording_id=recording_id,
                                reason="release group has no cover art")
    database.set_metadata_field(
        recording_id, "artworkPath", artwork, source=_ENRICHMENT_SOURCE,
        confidence=0.95,
    )
    database.clear_artwork_miss(key)
    database.set_enrichment_state(
        key, "resolved", resolver_version=RESOLVER_VERSION,
    )
    return EnrichmentResult("resolved", recording_id=recording_id,
                            reason="identity, release group and cover resolved")


def _release_groups_from_state(state: dict[str, Any] | None) -> list[ReleaseGroup]:
    if not state or not state.get("release_groups_json"):
        return []
    try:
        raw = json.loads(state["release_groups_json"])
    except (TypeError, ValueError):
        return []
    return [
        ReleaseGroup(
            id=str(item.get("id") or ""),
            title=str(item.get("title") or ""),
            primary_type=str(item.get("primary_type") or ""),
            secondary_types=list(item.get("secondary_types") or []),
        )
        for item in raw
    ]


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "", 0) else None
    except (TypeError, ValueError):
        return None


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != "" and value.strip().lower() not in {"unknown artist", "unknown title", "unknown album"}
    return bool(value)


def _temporary(database: Any, track_key: str, reason: str) -> EnrichmentResult:
    state = database.get_enrichment_state(track_key)
    attempts = int((state or {}).get("last_attempt_at") or 0)
    delay = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** min(attempts, 6)))
    database.set_enrichment_state(
        track_key,
        "temporary_failure",
        failure_code="retry",
        resolver_version=RESOLVER_VERSION,
        next_retry_at=int(time.time() + delay),
    )
    return EnrichmentResult("temporary_failure", reason=reason)


def _most_common(values: list[str]) -> str:
    """The most frequent value, or "" when nothing repeats enough to matter."""
    if not values:
        return ""
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    best = max(counts, key=counts.get)
    return best if counts[best] >= 3 else ""
