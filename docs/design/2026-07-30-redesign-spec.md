# Telegram Turntable — redesign spec

Date: 2026-07-30
Baseline: tag `pre-redesign-2026-07-30`, audit `2026-07-30-ui-audit.md`,
screenshots `docs/design/baseline/`
Direction: **dubplate / white label**. Devices: **desktop and phone equally**.

---

## 1. The subject, pinned

A self-hosted player that turns music posted in your Telegram channels, bots, private chats
and Saved Messages into a listenable library. One technical person, plus maybe a household,
on a laptop and a phone over the LAN. The page's single job: **find something to play among
thousands of files someone else dumped, and play it.**

## 2. Why "white label"

A dubplate or white label is an unmarked test pressing — no artwork, a stamped catalogue
number, information written on by whoever owns the record. That is precisely what this
library is: other people's rips with broken or missing metadata, annotated by you. The app
already stores a local metadata override; under this metaphor it stops being a "layer" and
becomes writing on the label.

The metaphor therefore has to do semantic work, not decoration:

| Product fact | Label idiom |
|---|---|
| Local metadata overrides | writing on a blank label |
| Missing cover art (common) | an actual white label — title stamped on blank paper |
| Sources are catalogues you browse | catalogue number + count in the header |
| Tracks arrive as dated posts | a numbered, dated run |
| Currently playing | the one thing wearing ink-stamp oxblood |

Anything that cannot be justified by that table is decoration and gets cut.

## 3. Tokens

### 3.1 Colour

```css
:root {
  --paper:      #faf9f6;  /* blank sleeve stock */
  --surface:    #ffffff;  /* raised: rows on hover, panels, modals */
  --ink:        #14110f;  /* stamp ink, near-black with a little brown */
  --graphite:   #6b655d;  /* secondary text */
  --rule:       #ddd8ce;  /* printed rule */
  --rule-soft:  #ebe7df;  /* row divider */
  --stamp:      #8c2f24;  /* oxblood. ONE job: currently playing. */
  --danger:     #a2331f;  /* destructive only */
  --scrim:      rgb(20 17 15 / .82);  /* over artwork; a token, not a literal */
}
```

Dark theme is black acetate, not inverted paper:

```css
--paper: #12100e; --surface: #1b1815; --ink: #f2efe8;
--graphite: #a39c92; --rule: #332e29; --rule-soft: #24211d;
--stamp: #d4574a; --danger: #e0796a;
```

Measured (WCAG 2.x, computed from these exact values):

| Pair | Ratio | Requirement | Verdict |
|---|---|---|---|
| `--graphite` on `--paper` | 5.47:1 | 4.5:1 text | pass |
| `--stamp` on `--paper` | 7.83:1 | 4.5:1 text, 3:1 non-text | pass |
| `--ink` on `--rule` (progress elapsed) | 13.2:1 | 3:1 non-text | pass |
| `--ink` vs `--graphite` (elapsed vs buffered) | 2.60:1 | separable | pass |
| `--graphite` on `--rule` (buffered vs remaining) | 4.06:1 | 3:1 non-text | pass |
| `--stamp` on dark `--paper` | 4.80:1 | 4.5:1 text | pass |
| `--graphite` on dark `--paper` | 7.66:1 | 4.5:1 text | pass |

**Rules of use, enforced by review:**
- `--stamp` appears only on the currently-playing track: its row, its label disc ring, the
  progress elapsed fill's cap, and the player's playing indicator. Nothing else, ever.
- Selection and hover are surface shifts (`--paper` → `--surface`), never colour.
- Focus is `--ink`: `0 0 0 2px var(--paper), 0 0 0 4px var(--ink)`.
- "On" states (shuffle, repeat) are an `--ink` fill with `--paper` glyph — a pressed
  stamp — not a tint.
- No hex literals outside the token block. `--scrim` exists so `.track-play-overlay` stops
  hardcoding light-mode ink.

### 3.2 Type

Three self-hosted variable faces, ~180 KB total in `static/fonts/`, declared with
`font-display: swap` and preloaded:

| Role | Face | Use |
|---|---|---|
| Display | **Archivo Condensed** | source titles, modal headings, stamped label text |
| Body | **Archivo** | everything readable |
| Data | **IBM Plex Mono** | durations, dates, counts, ordinals, catalogue numbers |

Archivo + Archivo Condensed is a superfamily pairing: real width contrast, no clash. The
condensed display face is the stamped-lettering reference; it is not a serif and not a
default. IBM Plex Mono is chosen over a display mono because most of its job is dense
tabular figures.

Scale — six steps, replacing eleven:

```css
--text-display: 44px;  /* clamp(32px, 5vw, 44px); source title, modal h2 at 28px */
--text-title:   22px;  /* panel titles, empty-state headlines */
--text-body:    15px;
--text-small:   13px;
--text-micro:   11px;  /* HARD FLOOR. nothing smaller ships. */
--text-data:    12px;  /* mono, font-variant-numeric: tabular-nums */
```

The 11px floor deletes every 8px and 9px string found in the audit (`.global-result-mark`,
`.queue-state`, `.cache-state`, `#repeat-badge`, `.global-search-signal small`,
`.global-result-group h3`).

Settings help text gets `font-weight: 400` explicitly, since it currently inherits 600.

### 3.3 Geometry

```css
--radius-control: 6px;   /* was 8 — a stamped edge is tighter */
--radius-panel:   12px;
--row-height:     52px;  /* was 68 */
--art-row:        40px;
--rail-width:     236px;
--panel-width:    360px;
--player-height:  84px;
```

---

## 4. Structure

### 4.1 The library is a numbered, dated run

The audit's central finding: the list is ordered by `sent_at DESC` and the date is never
shown, never labelled, and not sortable. Under this metaphor the fix and the identity are one
gesture.

```
┌──────────────────────────────────────────────────────────────────────┐
│ CHANNEL                                                              │
│ Hyperdub                                        412 tracks · 4m ago  │
│                                                                      │
│ [▶ Play]  [⤮ Shuffle]         [filter this catalogue]   [Sort ▾]  ⟳  │
│ ────────────────────────────────────────────────────────────────────  │
│  #    TRACK                             POSTED       TIME            │
│ ────────────────────────────────────────────────────────────────────  │
│ 001   ▣  Angels                         29 Jul       6:31     ♡   ⋯  │
│          Burial                                                      │
│ 002   ◉  Rival Dealer                   29 Jul      10:02     ♥   ⋯  │  ← stamp
│          Burial                                                      │
│ 003   ▣  Sines                          28 Jul       4:18     ♡   ⋯  │
│          Kode9 & The Spaceape                                        │
└──────────────────────────────────────────────────────────────────────┘
```

- **Ordinal** in mono at the left. It is the real play position, so the numbering carries
  information rather than decorating. Zero-padded to the width of the total.
- **POSTED** — `sentAt`, formatted relatively under a week ("2h ago", "Yesterday") and
  absolutely beyond it ("29 Jul", "29 Jul 25" across years). Never a raw `toLocaleString()`.
- **SOURCE column appears only in All music and Liked songs.** Inside one source it is
  redundant with the h1 and is the widest column in the table.
- **Sort control** on the track list: Posted (default) / Title / Artist / Duration, with the
  active key marked in the column header.
- In All music, day rules break the run (`── 29 JUL ──`) because recency is the meaningful
  axis there. Inside one source, no rules: you are reading a catalogue front to back.
- 52px rows, 40px art. ~13 rows per 900px viewport, up from 7.
- Row actions (like, menu) are **always rendered**, at `--graphite`, going `--ink` on
  hover — never `opacity: 0`. The audit's occlusion and hidden-count bugs both came from
  hiding real content behind hover.

### 4.2 The rail

- Source rows: avatar, title, **human kind label**, count. The count is a sibling of the
  title in the grid, not inside a hover overlay.
- Kind labels come from one formatter, used by rail, discover dialog and header alike:
  `channel → "Channel"`, `private → "Private chat"`, `saved → "Saved Messages"`,
  `bot → "Bot"`. Raw enum values never reach the DOM.
- Sync error is a separate mark (a `--danger` dot plus tooltip), not appended to the kind
  string as `bot · needs attention`.
- Per-row actions move into the row's `⋯` menu. Three unlabelled 27px icons — two of which
  are near-identical sync variants, one drawn as a repeat loop — do not survive.
- Pinned sources are grouped above a rule labelled `PINNED`, rather than marked by a 12px
  pale tick before the title.
- Collapsed rail: avatars only, `width: 100%; max-width: 46px` so no 1px overflow can spawn
  a scrollbar. The collapse chevron flips direction with the state.
- Bulk-select is a mode: hover actions are suppressed, "All music" and "Liked songs" are
  moved out of the selectable list, and the destructive verb is **"Remove from library"**,
  not "Unselect".

### 4.3 Now Playing

- **The header collapse must actually work** (audit A3). The guard drops
  `- header.offsetHeight`.
- Expanded header ≤ 45% of panel height; collapsed ≤ 22%.
- Tabs: Lyrics / Queue / Details, `--ink` underline on the active one, correct
  `aria-controls` / `aria-labelledby` wiring.
- **Queue** renders one of two things, never both: rows, or the empty state. The summary
  counts the whole queue, not just what follows the cursor.
- **Details** gains duration, date posted, track and disc number, and format — all already
  indexed, all currently dropped. Actions sit above the fold.
- Lyrics: active line `--ink` at `--text-title`, inactive `--graphite`. Attribution line
  ("Lyrics from LRCLIB") at `--text-micro`, with an edit affordance.

### 4.4 The signature: the label disc

The now-playing artwork becomes a record label.

```
   with artwork                     no artwork (the common case)
   ╭─────────────────────╮          ╭─────────────────────╮
   │    ╭───────────╮    │          │    ╭───────────╮    │
   │  ╱   artwork     ╲  │          │  ╱  ▔▔▔▔▔▔▔▔▔▔   ╲  │
   │ │   circular ( ) │  │          │ │  UNTITLED  ( )  │  │
   │  ╲   spindle    ╱   │          │  ╲  MIXDOWN     ╱   │
   │    ╰───────────╯    │          │    ╰───────────╯    │
   ╰─────────────────────╯          ╰─────────────────────╯
```

- Circular crop, a concentric ring at the label edge in `--rule`, and a real spindle hole:
  a `--paper` dot with an inset shadow.
- Rotates only while playing. **Not 33⅓ RPM** — that is 1.8s per revolution and reads as a
  fidget spinner. 20s per revolution: alive, not annoying. Holds position on pause.
- `prefers-reduced-motion: reduce` → static disc, no rotation, ever.
- **No artwork → an actual white label:** blank paper disc, title stamped across it in
  Archivo Condensed caps, clipped to the disc. This is the payoff — the product's weakest
  data state becomes its most characteristic image.
- This is the only bold element. Everything around it stays quiet. (Chanel's rule: one
  accessory. The disc is it.)

### 4.5 Mobile

Not an adaptation — a second designed layout.

- **Now Playing stops above the player**: `bottom: var(--player-height)` instead of
  `inset: 0`. The player stays visible and functional. This fixes the unreachable-transport
  bug structurally rather than by adding a second transport that has to stay in sync.
- Rows keep ordinal, title, artist, date. Duration and like move to the row's `⋯` sheet.
- **The playlist filter comes back** as a header field; it is currently `display: none`
  below 860px, which removes the narrowing task entirely on a phone.
- The track count stays; the decorative `CHANNEL` eyebrow is what drops at small widths.
- Row actions reachable via a bottom sheet with 44px targets, not 27px icons.
- Titles win the space fight: at 320px the ordinal is 3ch and the date is dropped before
  any title truncation.

### 4.6 Search results reuse library rows

One row system. Global search results get the same artwork, ordinal-free but same metrics,
same type sizes. The 8px `.global-result-mark` column — which currently means "Open",
"Preview", a duration, *or* the word "Telegram" depending on branch — splits into an explicit
`--text-micro` provenance tag ("In your library" / "On Telegram") plus a duration in the
normal duration column.

### 4.7 Copy rules

- No implementation language in user-facing strings. `LIBRARY LENS` and
  `LOCAL METADATA LAYER` are deleted.
- Eyebrows only where they classify. Keep `CHANNEL` over a source title and `SAVED LOCALLY`
  over Liked songs. Delete `PLAYER PREFERENCES`, `NOW PLAYING`, `NO MATCHES`,
  `TELEGRAM LIBRARY`, `TELEGRAM CONTACTS`, `PLAIN OR LRC`, `ONE-TIME CONNECTION`, `LOCKED`.
- Buttons name what happens, and the name persists through the flow.
- One word per idea: a queue is *empty*, never also *clear*.
- **No exception text in the UI.** `error.message` is shown only when the error originated
  server-side as an `AppError` with a user-facing message; anything else gets a written
  fallback plus a Retry when the operation is retryable.
- No shell commands in preference panes.
- Never state a number you do not have: uncounted sources show `—`, not `0 music files`.
- Headlines take no terminal period.

---

## 5. Out of scope

Deliberately not doing, to keep this shippable:

- No new playback features (no crossfade, gapless rework, EQ, speed control).
- No change to the sync engine, Telegram auth flow, or database schema. `sentAt`,
  `durationMs`, `trackNumber` etc. are already indexed; this only surfaces them.
- No component framework. Plain modules, as today.
- No visual change to the login/QR gate beyond tokens and copy — it is a one-time screen.
- The `app.js` file is not broken up wholesale. Only the render templates move, because that
  is where two of the confirmed bugs were hiding.

## 6. Success criteria

1. Every confirmed bug in the audit's section A is fixed, each with a test that fails
   against tag `pre-redesign-2026-07-30`.
2. No token pair in section 3.1 regresses below its stated ratio — enforced by a test that
   parses the CSS and computes contrast.
3. No rendered font-size below 11px — enforced by a test.
4. `--stamp` appears in exactly the permitted rules — enforced by a test.
5. No hex literal outside the `:root` blocks — enforced by a test.
6. On a 390px viewport with Now Playing open, the play button is hittable — enforced by a
   Playwright test.
7. The library shows a date and can be sorted; the source column is absent inside a source.
8. Backend tests (63) and player-core tests (2) still pass.
