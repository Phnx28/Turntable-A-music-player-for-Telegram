# Plan: Resolve diverged merge + fix code-review findings

**Trigger:** `/lfg fix them` following a full-codebase review.
**Repo state:** mid-merge of `origin/main` (`49ee4e9`, 17 commits) into local `main`
(`69ef873`, 21 commits). 5 files unmerged; frontend does not parse; test suite blocked.

## Goals

1. **Blocker — resolve the merge** so every source file parses:
   - `static/app.js` (13 hunks), `static/index.html` (4), `static/style.css` (27),
     `static/404.html` (1), `tests/test_layout.py` (4).
2. **Correctness fixes from the review:**
   - `core.py mark_missing_unavailable`: chunk the `NOT IN (...)` placeholder list
     (SQLite variable limit ~32k / 999 on old builds) or otherwise bound it.
   - `media.py MediaCache.document_cache`: bound its size (lazy expiry never evicts).
3. **Cosmetic:** stray whitespace `app.py:1001` (`"...musicbrainz/test"    )`) and
   `app.py:889` (`methods=["GET", "HEAD"]    )`).

## Resolution policy for conflicts

- HEAD side is the user's current line of work (newest commits, tab-indent style);
  incoming `49ee4e9` is remote polish in compressed 2-space style.
- Per hunk: keep whichever side carries the newer behavior, but **preserve any
  symbol introduced by the dropped side if it is referenced elsewhere**
  (verify by grep before dropping). Normalize formatting to HEAD's tab style.
- After resolution: `node --check static/app.js`, JS tests, full pytest, ruff check.

## Verification

- `node --check static/app.js` passes; `node --test` (or per-file) JS tests pass.
- `uv run pytest` green.
- `uv run ruff check .` clean.
- Merge committed with `git commit` (merge message documenting resolution choices).

## Shipping

- Push updated `main` to origin (merge makes history fast-forwardable from origin).
- Open no PR for the merge itself (direct-to-main repo); CI watched if workflows exist.
- Review fixes land as a separate commit on top.
