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
import logging
import time
from typing import Any, Literal

from acoustid import AcoustIDClient, AcoustIDError
from fingerprints import FingerprintError, FingerprintService
from resolver import ResolutionDecision, ResolutionKind, decide

LOGGER = logging.getLogger(__name__)

RESOLVER_VERSION = 1

# Temporary failures retry with exponential backoff, bounded.
RETRY_BASE_SECONDS = 30
RETRY_MAX_SECONDS = 24 * 60 * 60

# Source label for provenance rows written by this pipeline.
_ENRICHMENT_SOURCE = "fingerprint_resolver"

# Statuses that mean "never re-attempt this track automatically".
_TERMINAL = {"resolved", "ambiguous", "no_match", "manual_override"}


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
    ):
        self.database = database
        self.media = media
        self.fingerprints = fingerprints
        self.acoustid = acoustid
        # Playback-triggered runs in flight, keyed by track key, so rapid play events
        # never start a second fingerprint for the same track.
        self._in_flight: set[str] = set()

    async def enrich_track(
        self,
        track: dict[str, Any],
        *,
        trigger: Literal["playback", "bulk", "manual"],
        replace_existing: bool = False,
    ) -> EnrichmentResult:
        key = track["key"]
        if key in self._in_flight:
            return EnrichmentResult("skipped", reason="already in flight")
        self._in_flight.add(key)
        try:
            return await _enrich_track(
                self.database,
                self.media,
                self.fingerprints,
                self.acoustid,
                track,
                trigger=trigger,
                replace_existing=replace_existing,
            )
        finally:
            self._in_flight.discard(key)

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
    replace_existing: bool = False,
) -> EnrichmentResult:
    """Resolve *track* to a recording identity and persist it.

    *trigger* is recorded for policy/debugging; *replace_existing* lets the
    bulk UI ask for fingerprint-verified replacement of Telegram metadata
    (Phase H), while playback enrichment never replaces.
    """
    key = track["key"]
    state = database.get_enrichment_state(key)
    if state and state["status"] in _TERMINAL:
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
        return _apply(database, track, decision, replace_existing=replace_existing)
    except asyncio.CancelledError:
        # The state stays "fingerprinting"; startup recovery flips it to retryable.
        raise
    except Exception:
        LOGGER.exception("Enrichment pipeline failed for %s", key)
        return _temporary(database, key, "pipeline failure")


def _apply(
    database: Any,
    track: dict[str, Any],
    decision: ResolutionDecision,
    *,
    replace_existing: bool,
) -> EnrichmentResult:
    recording = decision.recording
    if decision.kind in {ResolutionKind.AMBIGUOUS, ResolutionKind.NO_MATCH}:
        database.set_enrichment_state(
            track["key"], str(decision.kind.value), resolver_version=RESOLVER_VERSION
        )
        return EnrichmentResult(str(decision.kind.value), reason=decision.reason)
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
    # arrive with the release resolver in Phase G). set_metadata_field itself
    # refuses to overwrite locked or higher-precedence values, and the display
    # layering keeps user overrides on top of these automatic values.
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
    database.set_enrichment_state(
        track["key"], "resolved", resolver_version=RESOLVER_VERSION
    )
    return EnrichmentResult(
        str(decision.kind.value), recording_id=recording_id, reason=decision.reason
    )


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
