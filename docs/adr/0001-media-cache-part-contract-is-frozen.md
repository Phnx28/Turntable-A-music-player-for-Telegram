# ADR-0001: The Media cache .part on-disk contract is frozen

Status: Accepted · 2026-08-03

## Context

The Media cache (see `CONTEXT.md`, "Media cache") downloads a Track's audio into a
digest-named `.part` file that is appended to, chunk-aligned on resume
(`offset -= offset % MEDIA_CHUNK_SIZE`), renamed to `.audio` on completion, and swept by TTL
and the eviction budget. Four separate code paths write, read, and delete these files
(`cache_media`, `partial_media`/`MediaSource.iter_range`, `_evict_cache_sync`,
`clean_partial_cache`, `clear_cache`), and a fifth re-derives the digest.

The 2026-08-03 architecture review considered changing this scheme (e.g. embedding the file
name in the identity, or dropping the alignment truncation to close a read race). Both were
rejected.

## Decision

The `.part` on-disk contract is frozen:

- cache filenames stay `{media_digest(key, media_identity(document_id, size))}.part` /
  `.audio`;
- the chunk-aligned resume truncation stays;
- the rename-on-completion and the `.missing`/TTL conventions stay.

The only permitted changes are inside the owning module (`media.py`), preserving names,
alignment, and lifecycle. A scheme change requires a migration that renames or re-downloads
half-finished files, and must be its own ADR.

The read-side truncation race (a resume realignment shrinking the file between a reader's
`stat` and `read`) is accepted and self-healing: the reader stops cleanly and the client's
one silent audio retry re-requests. Do not "fix" it by locking the read path.

## Consequences

- Half-downloaded files survive process restarts and upgrades.
- Eviction and cleanup globs stay correct.
- The race stays documented instead of silently "fixed" by a lock that would serialize all
  streaming against all downloads.
