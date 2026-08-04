# ADR-0003: No real Database schema split — façade seams only

Status: Accepted · 2026-08-03

## Context

`Database` (core.py) holds every store behind one class and one write lock: auth sessions,
the account, the library (Sources, Tracks, overrides, lyrics, playback history), and two
cache stores. The 2026-08-03 architecture review identified real seams (TrackStore,
SourceStore, PlaybackStore, CacheStore, AuthStore) and one concrete defect: `mark_unavailable`
does not invalidate the memoized Source track counts.

## Decision

Split `Database` **only as façade seams** — thin store objects sharing the same SQLite
connection and the same write lock — when a feature makes it worth it. Never split the schema
into separate databases or per-store connections.

Reasons:

- the migration system is append-only `user_version`; a real schema split needs a migration
  with no migration framework;
- the single write lock is what keeps `is_authorized` cheap on every `/api` request and keeps
  write serialization predictable for one owner.

The stale-count defect is fixed regardless of the seam question: `mark_unavailable` must
invalidate the memoized counts like every other availability-changing path.

## Consequences

- Reads keep the per-thread connection pattern; writes keep one lock.
- Future auth or cache changes stay cheap to reason about.
- An architecture review must not propose a real schema split without reopening this ADR.
