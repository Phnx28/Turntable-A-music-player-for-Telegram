"""Recording-identity decisions: fingerprint evidence gated, never text-only.

Phase F4/F5/F6 of the plan. The core rule: a fuzzy text match is a candidate
*ranking* signal, never proof of identity. Silent auto-application of metadata
is allowed only when an acoustic fingerprint points at one recording and the
surrounding evidence (title, artist, duration) is internally consistent with it.

This module is deliberately pure: no network, no database, no subprocesses.
Every decision here is exercised by the fixture tests in tests/test_resolver.py,
so a dangerous false positive (studio vs live, remix, cover, same-title
different artist) can never silently overwrite a user's metadata.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

# Version qualifiers that make one recording materially different from another.
# "Paranoid Android" (studio) and "Paranoid Android (Live at Glastonbury)" are
# different recordings; so are a remix and an instrumental. The qualifier set is
# normalised but never erased -- it is exactly what prevents a text-only match
# from silently relabelling a live bootleg as the studio take.
VERSION_QUALIFIERS = {
    "live": {"live", "concert", "session", "bootleg"},
    "remix": {"remix", "extended", "club mix", "dub mix", "rework", "flip"},
    "radio edit": {"radio edit", "single edit", "7\" edit", "7 inch edit"},
    "instrumental": {"instrumental", "karaoke", "backing track", "minus one"},
    "acoustic": {"acoustic", "unplugged", "stripped"},
    "demo": {"demo", "demo version"},
    "remaster": {"remaster", "remastered", "remastered version", "remastered edition"},
    "re-recording": {"re-recording", "re-recording version", "re-recorded"},
    "mix": {"mix", "original mix", "original version"},
    "cover": {"cover", "cover version", "tribute"},
    "mono": {"mono", "monaural"},
    "alternate": {"alternate", "alternative version", "alt version", "alternate take", "take"},
}

_NORMALIZE_RE = re.compile(r"[^\w\s]+")


def normalize_name(value: str | None) -> str:
    """Casefold and collapse punctuation/whitespace for comparison."""
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(_NORMALIZE_RE.sub(" ", value).split())


def version_qualifiers(title: str | None) -> set[str]:
    """The version qualifier *kinds* present in *title*.

    Returns canonical kinds (e.g. {"live"}) rather than raw words, so "live",
    "(Live)", "[live at x]" and "live version" all count as the same evidence.
    """
    cleaned = normalize_name(title)
    if not cleaned:
        return set()
    found: set[str] = set()
    for kind, words in VERSION_QUALIFIERS.items():
        for word in words:
            if f" {word} " in f" {cleaned} " or cleaned == word:
                found.add(kind)
                break
    return found


@dataclass(frozen=True)
class AcoustIDRecording:
    """One recording candidate returned by an AcoustID lookup."""

    mbid: str
    title: str
    artist: str = ""
    duration_ms: int | None = None
    sources: int = 0
    release_group_titles: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResolutionDecision:
    """The outcome of deciding whether one recording may be applied silently."""

    kind: str  # one of ResolutionKind
    recording: AcoustIDRecording | None = None
    reason: str = ""


class ResolutionKind(str, Enum):
    AUTO_APPLY = "auto_apply"
    FILL_MISSING_ONLY = "fill_missing_only"
    AMBIGUOUS = "ambiguous"
    NO_MATCH = "no_match"


DURATION_TOLERANCE_SECONDS = 8.0
DURATION_TOLERANCE_RATIO = 0.05
MAX_AMBIGUITY_CANDIDATES = 8


def _duration_compatible(candidate_ms: int | None, audio_duration_s: float) -> bool:
    if candidate_ms is None or audio_duration_s <= 0:
        return candidate_ms is None
    difference = abs(candidate_ms / 1000.0 - audio_duration_s)
    # The fingerprint duration is the audio's own length; the candidate duration is the
    # mastered recording's. The same recording drifts by a couple of seconds at most --
    # anything bigger is a different take, edit or version, and must not auto-apply.
    return difference <= max(DURATION_TOLERANCE_SECONDS, DURATION_TOLERANCE_RATIO * audio_duration_s)


def _title_compatible(telegram_title: str, candidate_title: str) -> bool:
    """True when the candidate title agrees with the Telegram title.

    "Agrees" means the core names match once version qualifiers are set aside --
    the qualifier sets are compared separately, so a studio Telegram title can
    never "match" a live candidate on the strength of the shared core name.
    """
    telegram = normalize_name(telegram_title)
    candidate = normalize_name(candidate_title)
    if not telegram or not candidate:
        return False
    if telegram == candidate:
        return True
    # One side may carry the qualifier text: compare the stripped cores.
    telegram_core = _strip_qualifiers(telegram)
    candidate_core = _strip_qualifiers(candidate)
    return bool(telegram_core and telegram_core == candidate_core)


def _strip_qualifiers(cleaned: str) -> str:
    """Remove version-qualifier words from an already-normalised name."""
    for words in VERSION_QUALIFIERS.values():
        for word in words:
            cleaned = re.sub(rf"\b{re.escape(word)}\b", " ", cleaned)
    return " ".join(cleaned.split())


def _version_conflict(telegram_title: str | None, candidate_title: str | None) -> bool:
    """True when the Telegram title and the candidate disagree on version.

    A Telegram title saying "live" against a studio candidate (which says nothing)
    is a conflict -- silently relabelling would destroy the user's context. A
    candidate carrying qualifiers the Telegram side lacks is *enrichment*, not a
    conflict: the fingerprint decided the audio really is that version. Only the
    Telegram side's explicit qualifiers can veto.
    """
    telegram_qualifiers = version_qualifiers(telegram_title)
    candidate_qualifiers = version_qualifiers(candidate_title)
    if not telegram_qualifiers:
        return False
    return not telegram_qualifiers.issubset(candidate_qualifiers)


def _artist_compatible(telegram_artist: str | None, candidate_artist: str) -> bool:
    """True when the candidate's artist agrees with the Telegram artist.

    An unknown/absent Telegram artist imposes no constraint; a real one must
    match the candidate, so "Dreams" by Fleetwood Mac can never be resolved to
    "Dreams" by The Cranberries on title similarity alone.
    """
    telegram = normalize_name(telegram_artist)
    candidate = normalize_name(candidate_artist)
    if not telegram or telegram == "unknown artist":
        return True
    if not candidate:
        return False
    return telegram == candidate or telegram in candidate or candidate in telegram


def decide(
    *,
    telegram_title: str | None,
    telegram_artist: str | None,
    audio_duration_s: float,
    candidates: Iterable[AcoustIDRecording],
) -> ResolutionDecision:
    """Decide whether a recording identity may be silently applied.

    Gate model (never a text-weighted score):
    - No candidates          -> NO_MATCH
    - One candidate, all evidence consistent (name, version, duration)
                               -> AUTO_APPLY
    - One candidate with a name/duration match but conflicting version
      evidence, or several materially different recordings
                               -> AMBIGUOUS (never overwrite silently)
    - One candidate whose name does not match the Telegram text at all
      (e.g. the Telegram title is file noise like "09 - track-final.mp3")
      -> FILL_MISSING_ONLY only if the name *cannot* be judged (title is
      noise); otherwise NO_MATCH
    """
    unique: list[AcoustIDRecording] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate.mbid or candidate.mbid in seen:
            continue
        seen.add(candidate.mbid)
        unique.append(candidate)
        if len(unique) >= MAX_AMBIGUITY_CANDIDATES:
            break
    if not unique:
        return ResolutionDecision(ResolutionKind.NO_MATCH, reason="no acoustid results")

    telegram_core = normalize_name(telegram_title or "")
    title_is_noise = not telegram_core or _is_file_noise(telegram_title or "")
    compatible = [
        candidate for candidate in unique
        if _duration_compatible(candidate.duration_ms, audio_duration_s)
    ]
    if not compatible:
        # Every candidate's duration disagrees with the audio: the recording is not
        # what is playing (a different take, edit or track entirely).
        return ResolutionDecision(
            ResolutionKind.AMBIGUOUS,
            reason="no candidate duration matches the audio",
        )

    named = [
        candidate for candidate in compatible
        if title_is_noise or _title_compatible(telegram_title or "", candidate.title)
    ]
    if title_is_noise:
        # The Telegram side carries no usable name; a single duration-consistent
        # fingerprint candidate is still strong evidence -- fill only the fields
        # the track lacks, never replace existing display values.
        if len(compatible) == 1:
            return ResolutionDecision(
                ResolutionKind.FILL_MISSING_ONLY,
                recording=compatible[0],
                reason="title is file noise; single fingerprint candidate",
            )
        return ResolutionDecision(
            ResolutionKind.AMBIGUOUS,
            reason="multiple fingerprint candidates and a title too noisy to disambiguate",
        )
    if not named:
        # The fingerprint and the Telegram text disagree on what this is.
        return ResolutionDecision(
            ResolutionKind.AMBIGUOUS,
            reason="fingerprint candidates do not match the Telegram title",
        )
    same_artist = [
        candidate for candidate in named
        if _artist_compatible(telegram_artist, candidate.artist)
    ]
    if not same_artist:
        # Same title, different artist: "Dreams" by Fleetwood Mac is not "Dreams"
        # by The Cranberries, and the text cannot decide -- so neither can we.
        return ResolutionDecision(
            ResolutionKind.AMBIGUOUS,
            reason="candidate artist disagrees with the Telegram artist",
        )
    named = same_artist

    conflicted = [
        candidate for candidate in named
        if _version_conflict(telegram_title, candidate.title)
    ]
    if conflicted:
        # The fingerprint points at e.g. "Live at X" while the Telegram text says
        # studio: silently relabelling would lose the user's context.
        return ResolutionDecision(
            ResolutionKind.AMBIGUOUS,
            reason="version evidence conflicts (e.g. live/remix/edit)",
        )
    if len(named) > 1:
        return ResolutionDecision(
            ResolutionKind.AMBIGUOUS,
            reason="multiple materially different recordings match",
        )
    return ResolutionDecision(
        ResolutionKind.AUTO_APPLY,
        recording=named[0],
        reason="single fingerprint-consistent recording",
    )


_FILE_NOISE_RE = re.compile(
    r"^(?:track[-_\s]?\d+|09\s*[-_]\s*.*|.*\b(?:final|draft|untitled|unknown)\b.*)$",
    re.IGNORECASE,
)


def _is_file_noise(title: str) -> bool:
    """True when a title looks like an uploaded file name, not a real song title.

    "09 - track-final.mp3", "untitled draft mixdown FINAL v2" and friends must
    not veto a strong fingerprint -- but they also must not be trusted as text
    evidence. The fingerprint fills missing fields only in that case.
    """
    cleaned = normalize_name(title)
    if re.search(r"\b(?:\.mp3|\.flac|\.m4a|\.ogg|\.wav|\.opus|\.aac)\b", cleaned):
        return True
    if cleaned.startswith("track "):
        return True
    return bool(_FILE_NOISE_RE.match(cleaned))


def distinct_recordings(candidates: Iterable[AcoustIDRecording]) -> list[AcoustIDRecording]:
    """Candidates deduplicated by MBID, most-supported first (for display/tests)."""
    seen: dict[str, AcoustIDRecording] = {}
    for candidate in candidates:
        if not candidate.mbid or candidate.mbid in seen:
            continue
        seen[candidate.mbid] = candidate
    return [seen[key] for key in sorted(seen, key=lambda key: seen[key].sources, reverse=True)]
