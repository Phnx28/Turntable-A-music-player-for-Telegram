# Kimi3 Thoughts — Telegram Music Player Audit

**Date:** 2026-07-27  
**Scope:** full-stack audit of `/home/kinofare/telegram-music-player-git`  
**Stack:** FastAPI + Telethon + SQLite · vanilla ES modules · no frontend build  
**Intent of this file:** honest opinions and a prioritized menu of changes.  
**Not this file:** an agent implementation brief. If we agree on direction, that becomes a second MD.

---

## 1. Overall verdict

This is a **serious, carefully made single-user product**, not a weekend hack. The bones are good:

- Correct HTTP Range / 206 streaming, resumable media cache, path-safe art/cache paths.
- Thoughtful domain model (local metadata overrides layered over Telegram, temporary sources, weighted shuffle, FTS5).
- Real UI craft (design tokens, elevation ladder, reduced-motion, safe-area, virtualized library).
- Security headers, origin checks, Fernet-encrypted Telegram session, login rate limit.
- A self-aware `TODO.md` that already names many of the same issues.

What holds it back is not “missing features,” it’s **a few high-leverage correctness/ops bugs**, **event-loop blocking under load**, **mobile daily-driver gaps**, and **monolith gravity** (`core.py` / `telegram_service.py` / `app.js`) that will make the next features cost more than they should.

**Bottom line:** keep the architecture. Fix the sharp edges. Then pick 2–3 product bets (PWA + mobile player + playlists, or gapless + current-track cache-first streaming). Don’t rewrite.

Confidence in findings below is high where I read the code path; medium where behavior depends on browser/Telegram runtime I couldn’t exercise live.

---

## 2. What the product is (and is not)

**Is:** a private web player that indexes audio from *your* Telegram channels/bots/DMs/Saved Messages, streams it with Range support, caches upcoming tracks, and lets you curate metadata/lyrics/likes locally.

**Is not:** multi-user SaaS, a group chat indexer, offline-first mobile app (yet), or a music library manager with albums/artists as first-class objects.

That product identity is strong. Don’t dilute it with a framework rewrite or a multi-tenant auth redesign unless the deployment model changes.

---

## 3. Architecture & file structure

### 3.1 Current shape

```
app.py                 # FastAPI factory, auth, all HTTP routes (~785)
core.py                # pure helpers + Database god-object (~1210)
telegram_service.py    # Telethon login, sync, media, jobs (~1126)
external.py            # MusicBrainz + LRCLIB + covers (~322)
static/
  index.html           # shell + every dialog (~408)
  app.js               # entire client (~1420 / ~94 KB)
  player-core.js       # pure helpers, tested (~64)
  style.css            # design system + all surfaces (~61 KB)
tests/test_core.py     # solid unit/integration for core + telegram bits
```

This is the right *kind* of split for a self-hosted single-user app: thin HTTP layer, domain DB, Telegram adapter, external metadata adapter, static client. No React tax. No build step. Docker is one service. Good.

### 3.2 Where structure is starting to hurt

| File | Problem | Opinion |
|------|---------|---------|
| `core.py` `Database` | ~40 methods: sources, tracks, FTS, lyrics, settings, playback, media_cache, lookup_cache | Split by store *when* the next feature lands, not as busywork. Suggested seams: `TrackStore`, `SourceStore`, `PlaybackStore`, `CacheStore`. Keep one `Database` facade if you want one connection. |
| `telegram_service.py` | login flows + sync + media cache + prefetch + avatars + share all in one class | Extract `MediaCache` (paths, resume, eviction, tagged download) and `BackgroundJobs`. Login/sync stay. |
| `app.js` | 1420 lines, many multi-statement lines, zero DOM tests | Split into ES modules *without* a bundler: `api.js`, `library.js`, `player.js`, `sources.js`, `lyrics.js`, `settings.js`, `ui.js`. `app.js` becomes wiring. |
| `app.py` routes as nested closures | hard to unit-test routes without full app; fine at this size | Keep. Add `TestClient` tests for auth/cookie/range; don’t invent a router class yet. |
| `style.css` | 1098 lines, one file | Keep one file. Optional later: critical gate CSS inline, app CSS deferred — only if Lighthouse complains. |

### 3.3 Language / stack opinion

| Choice | Keep? | Why |
|--------|-------|-----|
| Python 3.12+ / FastAPI / Telethon | **Yes** | Telethon is the right MTProto client; FastAPI fits async streaming. |
| SQLite + WAL + FTS5 | **Yes** | Perfect for single-user. Don’t migrate to Postgres unless multi-device multi-writer becomes real. |
| Vanilla JS + no build | **Yes for now** | Right call for this product. Add a bundler only if you add TypeScript or a component framework. |
| React / Svelte / Vue | **No** | Would rewrite months of careful DOM/a11y work for little gain. |
| TypeScript | **Optional later** | Nice if `app.js` is modularized first; don’t start with a TS migration. |
| aiosqlite / run DB in threads | **Yes, soon** | Biggest backend responsiveness win (see §5). |
| Redis / job queue | **No** | In-process jobs + SQLite are enough for one owner. |
| WebSocket / SSE for jobs | **Nice** | Polling works; SSE would cut chatter and feel snappier on sync. Not urgent. |

### 3.4 How the app should be *presented* on devices

Opinionated product stance:

1. **Primary surface stays a responsive web app** behind your reverse proxy. That matches self-host + Telegram session on one box.
2. **Make it a real PWA next** (manifest + service worker that caches the shell and serves *already-prefetched* audio from Cache Storage / the server’s media-cache). Installable on phone home screen. This is the highest-leverage “feels native” move without rewriting.
3. **Do not build Electron / Tauri / native iOS-Android** unless you personally need OS media keys offline more than a PWA gives you. Cost dwarfs benefit for a single-user Telegram indexer.
4. **Mobile must become a first-class daily driver**, not a shrunk desktop. Today mobile loses prev/next, volume, drag-reorder, and a proper expanded player sheet (see §8). Fix those before adding desktop-only chrome.
5. **Tablet:** current 860–1120 breakpoints are decent; treat tablet as “desktop with optional rail drawer,” not phone.

---

## 4. Strengths worth protecting

Do not “optimize away” these:

1. **Range streaming with HEAD, 206, 416** — correct and rare to get right.
2. **Resumable `.part` downloads aligned to chunk size** — excellent.
3. **Local metadata overrides that survive Telegram resync** — the product’s soul; tests cover it.
4. **Temporary sources** for search hits outside the library — clever UX.
5. **Weighted shuffle with recent-track tail** — better than pure random.
6. **Deferred FTS dirty set** — good instinct (needs crash safety, §6).
7. **Per-source sync locks + global semaphore(3)** — right concurrency model.
8. **CSP + origin check + httponly/samesite cookie + Fernet session** — solid baseline.
9. **Virtualized track list + scroll-idle cover loading** — necessary at library scale.
10. **Design system tokens, reduced-motion, forced-colors, skip-link, live regions** — above average for a personal app.
11. **Cookie rotation *intent*** (see §6 — implementation doesn’t achieve the claimed property).

---

## 5. Performance, efficiency, responsiveness

Ordered by real-world impact for a daily driver with a large library.

### P0 — Event loop blocked by sync SQLite

`Database` uses `sqlite3` + `threading.RLock` and is called **directly from async route handlers and Telethon event handlers**. A heavy `list_tracks` / FTS flush / `playback_queue` (full key dump) stalls *everything*, including the audio stream generator’s event-loop scheduling.

**Fix direction:**

- Short term: wrap hot read paths in `asyncio.to_thread(...)` (you already did this for `_evict_cache` — same pattern).
- Better: `aiosqlite` or a dedicated reader connection (WAL allows concurrent readers) + writer queue.
- Cap `playback_queue` payloads or window them (see P1).

This is the #1 backend responsiveness issue.

### P0 — Playing track is not cache-first

Prefetch warms *upcoming* tracks. The **current** track often streams live from Telegram via `iter_download` with 512 KiB chunks. Seeks and scrubbing re-open ranges against Telegram. That is the most plausible root of the intermittent *“couldn’t be streamed”* TODO.

**Fix direction:**

- On `playKey` / first audio request: start `cache_media(current)` in background immediately.
- Serve ranges from the growing `.part` / completed file whenever bytes are present (read-through cache).
- Only fall back to live Telegram for uncached offsets.
- On `<audio>` error: one automatic resume-from-`currentTime` retry before the error dialog.

This alone will make the player feel like a local library.

### P1 — Full-library queue materialization

`POST /api/playback/queue` returns **every** key. Frontend keeps the full array in memory and `JSON.stringify`s it into `localStorage` (debounced on position changes). At 50–100k tracks:

- multi-MB API responses
- main-thread jank on persist
- `localStorage` pressure (~5 MB limit)

**Fix direction:** windowed queue (e.g. keep ±200 around `queueIndex`, fetch more near edges), or server-side queue session id. Persist only `{source, mode, seed/order, index, position}`, not 100k keys.

### P1 — GZip on media responses

Starlette 1.3.1 `GZipMiddleware` has **no `Content-Range` skip**. Browsers usually send `Accept-Encoding: identity` for media, so you often get away with it — but any client that accepts gzip + Range can break byte semantics, and gzipping already-compressed audio is pure CPU waste.

**Fix:** exclude `audio/*`, `image/*`, and paths under `/api/tracks/*/audio|download|cover` (or disable middleware for those routes). Explicit is better than hoping for browser quirks.

### P1 — Sync SQLite + FTS flush on search

`_dirty_search_keys` can grow large during a big sync; first search after sync pays a large blocking flush. Also **no startup reconciliation** — process crash loses dirty keys → FTS permanently stale for those rows until re-upsert.

**Fix:** flush dirty keys on a background timer; on startup compare `tracks` vs `tracks_fts` counts (or rebuild if delta large); optional FTS triggers.

### P2 — Other efficiency notes

| Item | Note |
|------|------|
| `media_semaphore(4)` shared | Prefetch can contend with active stream. Prefer priority: playback > prefetch, or two pools. |
| `document_cache` eviction | Dict insertion-order pop is pseudo-LRU; on access you re-insert (good). Cap 128 is fine. |
| Thumbnail/list covers | Full Telegram thumbs for every row. Generate 64–96px JPEG/WebP server-side for list; keep full for now-playing. |
| `list_tracks` OFFSET | Fine to ~tens of thousands; keyset pagination later if needed. |
| Telethon `NewMessage()` unfiltered | Every account message → DB `get_source`. Keep an in-memory `selected_chat_ids` set. |
| `global_music_search` re-walks dialogs | Use discovery cache for source-name matches. |
| Static assets | No long-cache headers / fingerprint. Add `Cache-Control` + `?v=` or hashed names. |
| uvicorn extras | `uvicorn[standard]` (uvloop, httptools) is free speed. |
| `playback_queue` shuffle | Loads all history joins into Python; OK at personal scale after windowing. |
| Job progress polling | 1–1.8s polls are OK; SSE optional polish. |
| Docker image | No `ffmpeg` installed, but `tagged_download` shells out to ffmpeg → tagged downloads silently fall back. Install ffmpeg in the image or document the dependency. |
| httpx pool | Single client is good; optional explicit `limits=` if you parallelize metadata hard. |

### Frontend responsiveness

Already good: virtualization (80-row window), `content-visibility`, cover observer deferred while scrolling, `requestAnimationFrame` for library scroll, abort controllers on library/global/lyrics.

Remaining jank sources:

1. **`renderSources()` always `scrollIntoView`s the active source** — fires on every `loadPage` completion → rail scroll jumps. Only scroll on *user* source change.
2. **Full `innerHTML` rebuild of visible track window** on every window shift — acceptable, but avoid `force` re-render when only like-state of one row changed.
3. **Progress scrubbing sets `currentTime` on every `input`** → range spam on live streams. Throttle seek to rAF / commit on `change`.
4. **Huge queue persist** (above).

---

## 6. Bugs & correctness (actionable)

### Critical / high

1. **`APP_PASSWORD` missing from `.env.example` and defaults to `""`**  
   - `PasswordBody` requires `min_length=1`, so **no password can unlock** if unset.  
   - First-run following README is a silent lockout.  
   - **Fix:** document `APP_PASSWORD` in `.env.example` + README; refuse startup if empty (or auto-print a generated one-time password to logs on first run).

2. **Cookie “rotation” is security theater**  
   - Middleware re-issues a new cookie every API call.  
   - Validation is stateless HMAC + expiry — **old cookies remain valid until expiry**.  
   - Comment claims leaked tokens die after next request; they don’t.  
   - Cost: extra `Set-Cookie` on every response; concurrent requests race which cookie the browser keeps (all still valid).  
   - **Fix options:** (a) server-side session table / single active token version in SQLite, or (b) drop rotation and keep long-lived HMAC cookie + logout that can’t revoke (honest single-user model). Prefer (a) if the app is exposed beyond localhost.

3. **Spacebar double-toggle when focus is on a button**  
   - Global keydown handles Space; focused `<button>` also activates on keyup → play then pause.  
   - **Fix:** ignore when `event.target.closest("button, a, [role='button'], select")`.

4. **Attribute injection via `escapeHtml` in attributes**  
   - `escapeHtml` does not escape `"` / `'`. Used in `title=`, `aria-label=` with full channel/track titles (attacker-influenced via Telegram).  
   - CSP `script-src 'self'` blocks inline handlers, so practical XSS is mitigated — still broken HTML and defense-in-depth gap.  
   - **Fix:** `escapeAttr()` that encodes `& < > " '`.

5. **GZip + Range** — see §5. Guard explicitly.

6. **Intermittent stream errors** — treat as product bug; cache-first current track + one auto-retry (§5).

### Medium

7. **Mobile left/right resizers still `position: fixed` below 860px**  
   - Invisible 7px strip at `left: calc(var(--rail-width) - 4px)` intercepts touches mid-screen; still focusable.  
   - **Fix:** `display: none` under 860px (and when sidebar collapsed).

8. **FTS dirty set lost on crash** — §5.

9. **`.part` files not in 5GB eviction budget**; cleaned only at startup with 7-day TTL. Long-running container + aborted downloads = disk growth. Include `.part` in eviction / periodic cleaner task.

10. **`Settings.from_env` computes `api_hash` then ignores the variable** (line 62 vs 84). Harmless duplication; clean up.

11. **`tagged_download`**: no ffmpeg in Docker; no subprocess timeout; no cover embed. Either add ffmpeg to image or stop advertising tagged downloads.

12. **`renderSources` scroll snap** — §5.

13. **Seek spam while scrubbing** — §5.

14. **Empty `APP_PASSWORD` vs `hmac.compare_digest` length** — startup validation is cleaner than relying on pydantic min_length alone.

### Lower / polish correctness

15. MediaSession: no artwork, no `setPositionState` — lock-screen scrubbing incomplete.  
16. Progress / volume lack `aria-valuetext`.  
17. Context menu: no Shift+F10 / Menu key; no focus trap.  
18. TODO marks some test items `[x]` that still aren’t present (no `TestClient` suite, no `app.js` tests, no compose `healthcheck`). Treat checkboxes as aspirational drift — re-open them.

---

## 7. Security

### Good

- CSP is strict and appropriate for a no-inline-script app.
- Origin check on mutating `/api/*`.
- HttpOnly + SameSite=Strict cookie; `DEV_INSECURE_COOKIE` escape hatch documented.
- Fernet for Telegram string session; key file `0600`.
- Path traversal guards on artwork and media-cache paths.
- Cover download host allowlist (`coverartarchive.org` only).
- Login attempt limiter (5 / 15 min / IP) with dict sweep.
- Docs/redoc disabled.
- Non-root `USER app` in Dockerfile (TODO is stale here).
- `forwarded-allow-ips=127.0.0.1` (TODO claiming `*` is stale — good).

### Fix / decide

| Issue | Severity | Opinion |
|-------|----------|---------|
| Empty / undocumented `APP_PASSWORD` | High (ops) | Startup fail or generate. |
| Stateless cookie, no real revocation | Medium | Session table or accept risk. |
| Embedded default `api_id` / `api_hash` | Low–Med | Fine for convenience; document abuse surface (shared app rate limits). Prefer env override in prod. |
| Rate limit only on app login | Low | Telegram endpoints gated by app cookie; OK for single-user. |
| Share/forward only to contacts list | Good | Keep. |
| `escapeHtml` in attributes | Med (defense) | Fix. |
| No auth on static assets | OK | HTML is public; API is gated. Fine for password gate model. |
| “Remove app password” TODO | Product decision | If always behind Tailscale/VPN/mTLS, password is redundant friction. If ever on a public URL, **keep** password *and* real sessions. My vote: **keep password**, fix setup, add optional `AUTH_DISABLED=1` for trusted networks. |

---

## 8. UI/UX, design polish, responsive

### Design quality

The completed “Professional Craft Pass” in `TODO.md` shows. Light/dark, accent pastels, mono eyebrows, player elevation, lyrics reading surface — this already looks intentional. Don’t rebrand.

Remaining craft nits:

- Cache-state badges at **8px** are nearly unreadable — 10–11px mono.
- Source drag has no handle icon / `cursor: grab` is easy to miss (you set grab — good; add a grip glyph when sort=custom).
- Per-source sync spinner/error badge in the rail (global strip isn’t enough when many sources sync).
- Discover dialog needs a filter input (sort-only doesn’t scale past ~30 chats).
- Theme switch already transitions body colors — fine.
- Now-playing art crossfade exists for large/mini — good; list row art still pops a bit.

### Mobile / responsive — the weak spot for a “daily driver”

| Breakpoint behavior | Issue | Wanted |
|---------------------|-------|--------|
| ≤860px transport | **Prev/next/shuffle/repeat hidden** — only Play | Always show prev / play / next; modes in expanded sheet |
| ≤860px | Volume hidden | In expanded player |
| ≤860px | Resizers still active | Hide |
| ≤860px | HTML5 DnD dead on touch | Long-press menu actions: Move up/down; or pointer-based reorder |
| ≤620px | Library filter search hidden | Keep a compact filter or put it in a sheet |
| Player | No swipe-up now-playing sheet gesture | Cover tap works; add swipe-up from bar |
| `100%` height shell | iOS URL bar issues | Prefer `100dvh` for `html, body, .app-shell` |
| PWA | None | Manifest + icons + SW shell cache |
| Safe areas | Mostly handled | Keep auditing sheets/modals |

Mobile expanded player should feel like Apple Music’s mini → full transition: large art, transport, volume, queue peek, lyrics. You already have `now-panel` full-bleed — wire it as the primary mobile player chrome and **stop stripping transport controls**.

### Desktop UX gaps (product, not polish)

- No user playlists (only Liked + per-source).
- No album/artist browse (everything is source-chronological).
- Recently played data exists, no view.
- No multi-select on tracks (bulk like/queue/download).
- No backup/export of likes + overrides + lyrics (one corrupted `library.sqlite3` loses curation).
- No sleep timer / crossfade / gapless (nice-to-have after reliability).

### Accessibility

Foundations are good. Highest-value remaining:

1. Roving tabindex / arrow keys in virtualized track list (`tabindex="-1"` today).
2. Keyboard equivalents for reorder.
3. `aria-valuetext` on progress & volume.
4. Context menu keyboard open + focus trap.
5. Ensure sync strip announcements stay (you have `#sync-status` — good).
6. Decorative images: empty `alt=""` is correct if truly decorative; avatars falling back to initials spans are fine.

---

## 9. Features — what I’d build next (opinion)

Prioritized for **personal daily use**, not feature bingo.

### Tier A — reliability & mobile (do before new toys)

1. Cache-first current track + stream auto-retry  
2. Fix APP_PASSWORD / first-run  
3. Real session revocation **or** honest no-rotation  
4. Mobile transport (prev/next), hide resizers, `100dvh`  
5. Windowed queue + safer localStorage persist  
6. DB off the event loop  
7. ffmpeg in Docker **or** remove tagged-download path  

### Tier B — product depth

1. **PWA** (install + offline shell + cached audio)  
2. **User playlists**  
3. **Recently played** view (data already there)  
4. Export/import library curation (JSON dump of overrides/likes/lyrics)  
5. Album/artist grouping (derived index, not a new source of truth)  
6. Gapless via dual `<audio>` or Web Audio once cache-first is solid  

### Tier C — delight

1. Crossfade / sleep timer / speed is **already implemented** in UI — expose consistently  
2. ReplayGain  
3. Desktop notification on track change  
4. Smart shuffle knobs in settings  
5. Contact share frequency / recommended row  

### Explicitly deprioritize

- Multi-user accounts  
- Group chat indexing  
- Rewrites to React/Postgres  
- Fancy EQ before mobile prev/next exists  

---

## 10. Testing & ops

| Gap | Opinion |
|-----|---------|
| No CI | Add GitHub Actions: `uv run python -m unittest` + `node --test static/*.test.js` on PR. Cheap. |
| No FastAPI `TestClient` tests | Add for: login rate limit, cookie auth 401, range traversal on cover, range 206/416, empty APP_PASSWORD startup. |
| `app.js` untested | After module split, unit-test pure helpers; smoke-test with Playwright later if you care. |
| compose healthcheck | `healthcheck: curl -f http://localhost:8000/healthz` — one-liner. |
| README | Too thin for first-run (password, MusicBrainz, ffmpeg, reverse proxy TLS, `DEV_INSECURE_COOKIE`). |
| Backup story | Document “back up `DATA_DIR`”; better: in-app export. |

---

## 11. Suggested roadmap (if we agree)

Not tickets — themes. A later agent brief would explode these into files/functions.

### Phase 0 — Stop the bleeding (1–2 days)

- Document + enforce `APP_PASSWORD`
- Spacebar guard; `escapeAttr`
- Hide resizers ≤860px; show prev/next on mobile
- Exclude media from GZip
- ffmpeg in Dockerfile **or** feature-flag tagged download
- compose healthcheck
- Fix `renderSources` scrollIntoView spam

### Phase 1 — Feels local (3–5 days)

- Cache-first playback + audio error retry
- `asyncio.to_thread` / aiosqlite for hot DB paths
- Windowed queue + slim player state persist
- FTS crash-safe flush
- MediaSession artwork + position state
- Seek throttle

### Phase 2 — Daily driver mobile + PWA (1 week)

- Expanded mobile player sheet with full transport + volume
- Touch-friendly reorder alternatives
- Web app manifest + icons + service worker (shell + optional audio cache)
- `100dvh` shell

### Phase 3 — Structure for growth (ongoing, opportunistic)

- Split `app.js` modules
- Extract `MediaCache` from `telegram_service`
- Split `Database` stores when a feature touches them
- CI + TestClient auth/range tests
- Playlists + recently played + export

### Phase 4 — Library power features

- Album/artist browse, multi-select, batch metadata, gapless

---

## 12. What I would *not* do

1. **Rewrite in another framework/language** — zero user value.  
2. **Add Postgres/Redis** for one user — complexity without payoff.  
3. **Remove the app password without a clear trust boundary** — optional flag only.  
4. **Big visual rebrand** — identity is already set; polish gaps, don’t restyle.  
5. **Implement every TODO checkbox** — many are nice; Tier A/B above is the path.  
6. **Server-side React SSR** — nonsense for this app.

---

## 13. File-level “if we touch it” cheat sheet

| Area | Primary files |
|------|----------------|
| Auth / cookie / password | `app.py` (`Settings`, `_make_cookie`, login), `.env.example`, README |
| Streaming / cache-first | `telegram_service.py` (`iter_media`, `cache_media`, `cached_media`), `app.py` (`media_response`) |
| Queue windowing | `core.py` (`playback_queue`), `static/app.js` (`libraryQueue`, `playerSnapshot`) |
| DB non-blocking | `core.py` + call sites in `app.py` / `telegram_service.py` |
| Mobile player | `static/index.html`, `style.css` (860/620 breakpoints), `app.js` transport |
| XSS attr escape | `static/app.js` (`escapeHtml` / new `escapeAttr`) |
| GZip exclude | `app.py` middleware setup |
| PWA | new `static/manifest.webmanifest`, SW, `index.html` link tags |
| Docker/ffmpeg/health | `Dockerfile`, `compose.yaml` |
| FTS durability | `core.py` (`_dirty_search_keys`, startup) |

---

## 14. Closing opinion

You already built the hard part: a tasteful self-hosted Telegram music client with real streaming, curation, and craft. The gap between “impressive project” and “I trust this every day on my phone” is mostly:

1. **stream reliability (cache-first current track),**  
2. **mobile controls,**  
3. **first-run/auth honesty,**  
4. **not blocking the async loop on SQLite,**  
5. **not shipping 100k keys through JSON/localStorage.**

Everything else is either modularization for sanity or features that can wait.

---

## 15. How we use this file next

- **Agree / disagree** on Phases 0–2 (and the PWA vs native stance).  
- Call out anything you explicitly don’t want (e.g. keep full queues, drop app password, no PWA).  
- Then we write a second MD — agent-ready, with concrete file targets, function names, acceptance checks, and order of commits — **only for the agreed slice**.

I’m not implementing from this document until you say which phases/items are in.