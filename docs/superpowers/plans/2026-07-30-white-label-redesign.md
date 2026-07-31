# White-label redesign — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Location:** this file. It supersedes the placeholder path in the handoff; commit it together with the untracked `docs/` tree (audit, spec, handoff, 31 baseline screenshots) as task 0.

## Context

`docs/design/2026-07-30-ui-audit.md` drove every surface of this self-hosted Telegram music player at six widths in both themes and found eleven functional defects, of which ten are confirmed by computed styles or geometry probes (A11 was tested and **retracted** — `content: url()` on an `<img>` works in Chromium *and* Firefox; do not resurrect it). `docs/design/2026-07-30-redesign-spec.md` is the approved response: a **dubplate / white-label** direction where the metaphor does semantic work, with desktop and phone as two designed layouts rather than one adapted.

The problem is not decoration. On a phone, opening Now Playing covers the transport so playback becomes unreachable; the Queue tab contradicts itself three ways at once; the collapsing header's guard is arithmetically unsatisfiable so it has never once fired; per-source track counts are visible in *no* state; the focus ring computes to 1.38:1 against a 3:1 requirement; and the progress bar shows neither position nor buffered extent at rest. The intended outcome is that every confirmed bug is fixed with a test that fails against tag `pre-redesign-2026-07-30`, and the visual identity is rebuilt on tokens whose contrast is enforced by a test rather than asserted in prose.

**Two corrections to the governing documents, both measured, both approved by the user.** The plan uses the measured values:

1. **The audit's A3 fix is wrong.** It says "drop `- header.offsetHeight`". Collapsing the header *grows* `#now-content`'s clientHeight (the header is a flex **sibling** — `index.html:210` vs `index.html:231`), which is exactly the oscillation the comment at `app.js:1349-1365` describes and which is real. The subtraction stays; it must subtract only the height the collapse *frees*.
2. **The handoff's `COMPACT_HEADER_HEIGHT = 72` is wrong.** Measured with the stylesheet actually loaded:

   | Viewport | Panel | Expanded header | Compact header |
   |---|---|---|---|
   | 1440×900 | 812 | 407 (50%) | **132** (16%) |
   | 390×844 | 844 | 515 (61%) | **120** (14%) |

   Against the audit's own measured case (`scrollHeight 636`, `clientHeight 206`, `headerHeight 546`): the old arithmetic gives −116 against a `> 48` test (the bug); the handoff's 72 gives **−44, so it still never fires**; the measured 132 gives +16, which clears a 12px floor but not 48. The fix therefore needs the measured, breakpoint-aware constant **and** the post-collapse threshold lowered from 48 to 12.

**Two ratios in the spec's own contrast table are arithmetic slips.** I recomputed all seven from the spec's exact hex values. Five verify. Two do not — and neither is reproducible from any other plausible pairing, so they are transcription errors, not different measurements:

| Pair | Spec claims | Actually computes | Requirement | Verdict |
|---|---|---|---|---|
| `--ink` vs `--graphite` | 2.60:1 | **3.26:1** | separable | still passes |
| `--graphite` on dark `--paper` | 7.66:1 | **6.99:1** | 4.5:1 | still passes |

**Both err in the safe direction — the palette itself is fine, only the printed numbers are wrong.** Task 12 must therefore assert *computed* ratios against their *requirements*, never the spec's transcribed figures, or it would enshrine two wrong constants. Reference values I computed for the token test: ink-on-paper 17.86, ink-on-surface 18.80, danger-on-paper 6.58, dark ink-on-paper 16.53, dark danger-on-paper 6.43, dark ink-on-rule 11.70, dark graphite-on-rule 4.94.

**One bug found by measurement that the audit missed** (now Task 3): `list_tracks`' SELECT omits `t.liked_at`, so `_track_summary`'s `value.get("liked_at")` is always `None` and **every library row renders un-liked**. `track_summaries` does select it, which is why the queue shows hearts correctly and the library never does.

**Goal:** Fix every confirmed functional defect, then rebuild the visual system on white-label tokens — without a schema change, a new dependency, or a component framework.

**Architecture:** Four phases. Phase 1 (tasks 1–11) is correctness with no intended visual change and is independently shippable; pure logic moves out of `app.js` into small testable modules (`player-core.js`, a new `format.js`) and a Playwright harness (`tests/test_layout.py`) becomes the standing guard for geometry claims. Phase 2 (12–15) writes a token-contract test **first**, then swaps the palette, self-hosts fonts, and rebuilds the dark theme against it. Phase 3 (16–22) is structure: the library becomes a numbered, dated, sortable run. Phase 4 (23–24) adds the signature label disc and the mobile layout.

**Tech Stack:** Vanilla ES modules (no framework), FastAPI + SQLite (WAL) with `json_extract` for metadata overrides, `unittest` + Playwright (sync API) for Python, `node:test` + `node:assert/strict` for JS. No new runtime dependency is added by this plan.

## Global Constraints

- **Test commands are exactly** `./.venv/bin/python -m unittest discover -s tests` and `node --test static/*.test.js`. **pytest is NOT installed.** `node --test static/` fails with "Cannot find module" — the glob form is mandatory.
- Baseline: **63 Python tests OK, 2 node tests pass** at tag `pre-redesign-2026-07-30` (commit `23f3413`). `55773d9` is the *annotated tag object* SHA, not a commit — do not confuse them. The tag stays **local; do not push**.
- **`tests/test_core.py` already exists — extend it, never create it.** Same for `static/player-core.test.js`.
- **The `sort` parameter must use a server-side allowlist. Never interpolate client input into SQL.**
- Do not copy credentials to new locations. The backup deliberately excludes `.env` (Telegram API credentials) and `data/` (contains `encryption.key`).
- `app.py:299` mounts `/assets` → the `static/` dir. Fonts live at `static/fonts/…`, are served at `/assets/fonts/…`, and must be referenced from `style.css` **relatively** as `url("fonts/…")`. A Playwright fixture serving `static/` **must rewrite `/assets/*` → `/*`** or every stylesheet 404s (this failure was hit and confirmed).
- **Never assert a visual fact you have not measured.** Three findings in the audit's first pass were wrong because a *stub* was wrong; two published claims had to be retracted. Probe computed styles or geometry before writing a claim.
- Duplicated CSS that must be changed in **both** copies or the fix does nothing: `.source-link` (style.css:358 **and** 947); `.now-panel { inset: 0 }` (~872-886 **and** 1222); `.transport .mode-button { display: none }` (893 **and** 1233); `.row-menu, .track-row-actions { opacity: 1 }` (915 **and** 1239). Likewise the two dark token blocks (`html[data-theme="dark"]` 33-47 and the `prefers-color-scheme` copy 50-66) — change together or the system theme silently diverges.
- `html[data-font="serif"|"mono"]` (style.css:68-69) remap `--font-ui`. The font work must not break them.
- The comment at style.css:491 states "Background only… **no left bar anywhere**" and **three rules break it**, in two different properties — so grepping for one form misses the third: `box-shadow: inset 3px 0` at **style.css:1002** (`.sidebar-collapsed .source-link.active`) and **style.css:1087** (`.queue-row.current`), plus `border-left: 3px solid var(--accent)` at **style.css:1390** (`.restart-notice`). All three verified.
- Stub field names that were wrong the first time and cost real debugging: jobs use **`processed`**, not `done`; metadata candidates come back as a **bare array**, not a wrapped object; `cache_status` returns `{"bytes","files","states"}` — **not `count`** (verified at telegram_service.py:1266-1271: `"bytes": sum(...), "files": len(entries), "states": states`).
- ⚠️ **The audit rig `seed.js` still contains that bug — do not port it verbatim.** `/home/kinofare/.playwright-mcp/seed.js` line ~66 stubs `/api/cache/status` as `{ bytes: 184_320_000, count: 41, states: {} }`. `count` is exactly the wrong field that produced the retracted `undefined cached` finding. When porting to Python, stub **`files`**. The handoff says "port the field names verbatim; they are the ones that produced correct renders" — that is true of the source and track shapes and false of this one. Re-check each stub against its real producer rather than trusting the rig.
- Source kinds are exactly `{"channel", "bot", "private", "saved"}` (telegram_service.py:602). Raw enum values never reach the DOM. **Raw `source.kind` currently reaches the DOM in four places** — the audit named three: `app.js:553` (rail `<small>`), `app.js:562` (`#source-kind`, the header eyebrow), `app.js:796` (global search results), `app.js:1183` (discover dialog). All four route through `sourceKindLabel`.
- **`.track-head` and `.track-row` share one grid declaration** (style.css:469-475, `grid-template-columns: minmax(220px, 1.5fr) minmax(130px, .75fr) 64px 36px 46px; column-gap: 18px`), with responsive overrides at style.css:841-842 and a `.track-head { display: none }` at style.css:911. Adding the ordinal and POSTED columns means editing that shared rule, its overrides, and the head markup at `index.html:194-196` together.
- **Must survive (audit §F):** `vector-effect: non-scaling-stroke` (style.css:121-126, with its recorded reasoning); the `prefers-reduced-motion` honourings — **14 in total, which is not a contradiction of the audit's "eleven": exactly 11 are in `app.js` and 3 more in `style.css`** (verified by count). Task 23's disc adds another and must match the existing `matchMedia("(prefers-reduced-motion: reduce)").matches` pattern used at `app.js:1381` and `app.js:1423`; share undo-with-delay; the Account danger-zone pattern; "Who can reach this player"; the filter-no-match empty state; glyph-fill liked state; mono `tabular-nums`.
- **Out of scope (spec §5):** no schema change, no new playback features, no component framework, no wholesale break-up of `app.js` (only render templates move), no visual change to the login/QR gate beyond tokens and copy.
- **`.form-actions` is used in seven places**, not only the metadata dialog: `index.html:323` (as the compound `form-actions full-row`), `:345`, `:392`, `:427`, `:438`, and the error and confirm dialogs at `:467` and `:468`. So Task 6's `align-items: center` is a **global** change — intended, but verify the alert dialogs and settings rows still sit correctly, and note `.metadata-form .inline-choice` is the *scoped* half of that fix.
- The error dialog's markup title is `Something went wrong` (`index.html:467`, `#error-title`) while `showError`'s default parameter is `"Couldn’t complete that"` (`app.js:81`) — two different strings for the same dialog. Task 11 should reconcile them rather than add a third.
- The label disc's target is `.large-art-wrap` (`index.html:217`, rule at `style.css:573-580`), which **already has `position: relative`, `aspect-ratio: 1`, `overflow: hidden`** — so the disc is a `border-radius: 50%` swap plus ring/spindle pseudo-elements, not new geometry. It contains `#large-art` (the `<img>`, `hidden` until loaded) and `#large-art-placeholder` — the latter *is* the no-artwork state the white label replaces. Compact sizes are pinned at `style.css:546` (64px) and `style.css:884` (56px), and `style.css:562` already sets `will-change: transform` on it for the FLIP.
- Copy strings the audit cites live in **`app.js`, not `index.html`** — a grep over markup alone will miss them: the shell command is `app.js:1697` (`"Started without run.py, so this setting is not applied. Restart with: uv run python run.py"`); `N cached` with no noun is `app.js:1664` **and** duplicated at `app.js:2083`; `"Telegram contact"` for Saved Messages is `app.js:1560`. The `#bind-restart-notice` element (`index.html:401`) is empty in markup and filled from JS.
- Ponytail (full) is in effect: fewest files, shortest working diff, root-cause fixes over per-caller patches, one runnable check per non-trivial change. Mark deliberate shortcuts with a `ponytail:` comment naming the ceiling.

## Sequencing and parallelism

- **`static/app.js:1` (the `player-core.js` import) is edited by both Task 1 and Task 2**, as is line 3 of `static/player-core.test.js`. Each task shows the full resulting line and says "extend, do not add a second import", so running them **in order** is clean — running them in parallel worktrees conflicts on that one line.
- **Test counts assume order.** Task 1 expects "3 tests pass", Task 2 "4". Landing them out of order shifts the numbers by one. The Python count moves 63 → 64 only in Task 3.
- **Task 3 shares no files with Tasks 1–2** and is the safe one to parallelise.
- **Task 12 must land before 13–15**: it is the contract those tasks are written to satisfy.
- **Task 8 gates the visual assertions.** Tasks 5, 6, 9 and 10 are CSS-only fixes that cannot be unit-tested in node; they land with a documented manual probe and their assertions are added to `tests/test_layout.py` once Task 8 exists.

## File Structure

| File | Responsibility | Phase |
|---|---|---|
| `static/player-core.js` (69 → ~110 lines) | Pure, testable player logic. Gains `queueView`, `shouldCompactHeader`. | 1 |
| `static/format.js` **(new)** | Pure display formatting: `sourceKindLabel`, `errorCopy`, `formatPostedDate`, ordinal padding, sync timestamps. The one place raw enums and raw dates are turned into human strings. | 1, 3 |
| `static/format.test.js` **(new)** | Guards `format.js`. | 1, 3 |
| `static/player-core.test.js` (41 lines) | Extended, never replaced. | 1 |
| `static/design-tokens.test.js` **(new)** | The Phase 2 contract, written **first**: WCAG ratios, 11px floor, `--stamp` allowlist, no stray hex. Plain regex over the token blocks — no CSS parser dependency. | 2 |
| `tests/test_layout.py` **(new)** | Playwright geometry harness; `unittest` + `skipIf` on import failure. The standing guard against unmeasured visual claims. | 1, 4 |
| `tests/test_core.py` (exists) | Extended with the liked-state and `sort` allowlist cases. | 1, 3 |
| `static/app.js` (2113 lines) | Render templates and wiring only; logic moves out to the modules above. | all |
| `static/style.css` (1414 lines) | Tokens, then the visual system. | 2–4 |
| `core.py` (1447) / `app.py` (937) | `t.liked_at` in the SELECT; the `sort` allowlist. | 1, 3 |

---

## Phase 1 — correctness, no visual change, independently shippable (tasks 1–11)

### Task 1: A2 — the Queue tab renders rows or the empty state, never both

**Files:**
- Modify: `static/player-core.js` (append after line 69)
- Modify: `static/app.js:1` (import line), `static/app.js:1084-1101` (`renderQueue`)
- Test: `static/player-core.test.js` (append a new `test(...)` block)

**Interfaces:**
- Consumes: nothing
- Produces: `queueView(queue: string[] | unknown, queueIndex: number) -> { total: number, upcoming: number, isEmpty: boolean, summary: string }` — a pure export from `static/player-core.js`. Task 2 imports from the same module and must extend the same import lines, not replace them.

Mechanism being fixed (both confirmed in the audit, section A2):
- `static/app.js:1089` counts only `upcoming` (`state.queue.slice(state.queueIndex + 1)`), so the currently-playing item is never queue content and the header reads "Your queue is empty" while a row says "PLAYING".
- `static/app.js:1099` concatenates the empty state **after** `rows` (`rows + (upcoming.length ? "" : '<div class="queue-empty">…')`) instead of choosing between them.
- Copy: spec §4.7 — "One word per idea: a queue is *empty*, never also *clear*" and "Headlines take no terminal period." The empty-state `<strong>Your queue is clear.</strong>` becomes `<strong>Your queue is empty</strong>`.

- [ ] **Step 1: Write the failing test**

Append to `static/player-core.test.js`, and extend the import on line 3 to include `queueView` (keep the existing alphabetical order):

```js
import { adjacentIndex, bufferedPercent, formatTime, lyricIndex, normalizePlayerState, normalizeTrackPage, queueView } from "./player-core.js";
```

```js
test("the queue summary counts the whole queue, and empty means empty", () => {
  // The regression: one track playing, nothing after it. The old summary counted only what
  // followed the cursor, so the header said "empty" above a row that said "PLAYING".
  const single = queueView(["1:2"], 0);
  assert.equal(single.isEmpty, false);
  assert.equal(single.total, 1);
  assert.equal(single.upcoming, 0);
  assert.equal(single.summary, "1 in queue · last track");

  const middle = queueView(["1:2", "1:3", "1:4"], 0);
  assert.equal(middle.total, 3);
  assert.equal(middle.upcoming, 2);
  assert.equal(middle.summary, "3 in queue · 2 up next");

  // Nothing has been played yet: the cursor sits before the first item.
  assert.equal(queueView(["1:2", "1:3", "1:4"], -1).upcoming, 3);
  assert.equal(queueView(["1:2", "1:3", "1:4"], -1).summary, "3 in queue · 3 up next");

  // A cursor past the end is a stale index, not negative upcoming.
  assert.equal(queueView(["1:2", "1:3", "1:4"], 99).upcoming, 0);
  assert.equal(queueView(["1:2", "1:3", "1:4"], 99).summary, "3 in queue · last track");

  // Genuinely empty, and the non-array case an unparsed snapshot can produce.
  assert.equal(queueView([], 0).isEmpty, true);
  assert.equal(queueView([], 0).summary, "Your queue is empty");
  assert.equal(queueView(null, 0).isEmpty, true);
  assert.equal(queueView(undefined, undefined).summary, "Your queue is empty");

  // Thousands separators, matching the .toLocaleString() the summary already used.
  assert.equal(queueView(Array.from({ length: 54660 }, (_, index) => String(index)), 0).summary,
    "54,660 in queue · 54,659 up next");
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test static/*.test.js`
Expected: FAIL with `SyntaxError: The requested module './player-core.js' does not provide an export named 'queueView'`

- [ ] **Step 3: Add `queueView` to `static/player-core.js`**

Append after line 69:

```js
export function queueView(queue, queueIndex) {
  const total = Array.isArray(queue) ? queue.length : 0;
  const cursor = Math.max(-1, Math.min(Number.isFinite(queueIndex) ? queueIndex : -1, total - 1));
  const upcoming = Math.max(0, total - cursor - 1);
  const summary = !total ? "Your queue is empty"
    : upcoming ? `${total.toLocaleString()} in queue · ${upcoming.toLocaleString()} up next`
    : `${total.toLocaleString()} in queue · last track`;
  return { total, upcoming, isEmpty: total === 0, summary };
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `node --test static/*.test.js`
Expected: PASS (3 tests pass)

- [ ] **Step 5: Wire it into `renderQueue`**

`static/app.js:1` — extend the existing import, do not add a second import statement:

```js
import { adjacentIndex, bufferedPercent, formatTime, lyricIndex, normalizePlayerState, normalizeTrackPage, queueView } from "./player-core.js";
```

`static/app.js:1084-1101` — replace the whole function. Only lines 1088-1089 and 1099 change in substance; `visibleStart`/`visibleEnd`/`rows`/`ensureSummaries` are unchanged:

```js
const QUEUE_EMPTY = '<div class="queue-empty"><strong>Your queue is empty</strong><span>Choose a track or add one from its more menu.</span><button class="button" type="button" data-queue-browse>Browse library</button></div>';

function renderQueue() {
  const historyStart = Math.max(0, state.queueIndex - state.historyVisible);
  const visibleStart = historyStart;
  const visibleEnd = Math.min(state.queue.length, state.queueIndex + 101);
  const view = queueView(state.queue, state.queueIndex);
  $("queue-summary").textContent = view.summary;
  if ($("queue-pane").hidden) return;
  const visible = state.queue.slice(visibleStart, visibleEnd);
  const rows = visible.map((key, offset) => {
    const summary = state.summaryCache.get(key); const detail = state.trackCache.get(key); const index = visibleStart + offset;
    const title = summary?.title || detail?.metadata?.title || "Loading track…";
    const artist = summary?.artist || detail?.metadata?.artist || "";
    const section = index < state.queueIndex ? "Played" : index === state.queueIndex ? "Playing" : "Up next";
    return `<div class="queue-row ${index < state.queueIndex ? "played" : ""} ${index === state.queueIndex ? "current" : ""}" draggable="${index > state.queueIndex}" data-queue-index="${index}" data-queue-key="${escapeHtml(key)}"><button class="queue-copy" type="button" data-queue-play="${index}"><span class="queue-state">${section}</span><strong>${escapeHtml(title)}</strong><small>${escapeHtml(artist)}</small></button><span>${index > state.queueIndex ? `<span class="cache-state ${state.cacheStates[key] || ""}">${escapeHtml(state.cacheStates[key] || "queued")}</span><button class="icon-button" type="button" data-remove-queue="${index}" aria-label="Remove from queue">${icon("close")}</button>` : ""}</span></div>`;
  }).join("");
  // Rows or the empty state, never both: the old form concatenated them, so a one-track queue
  // showed a PLAYING row with "your queue is clear" underneath it.
  $("queue-list").innerHTML = view.isEmpty ? QUEUE_EMPTY : rows;
  ensureSummaries(visible);
}
```

The static default in `index.html:233` (`<span id="queue-summary" class="small-copy">Your queue is empty</span>`) is already correct and needs no change.

- [ ] **Step 6: Verify both suites**

Run: `node --test static/*.test.js`
Expected: PASS (3 tests pass)

Run: `./.venv/bin/python -m unittest discover -s tests`
Expected: PASS, `OK` with 63 tests (this task touches no Python)

- [ ] **Step 7: Commit**

```bash
git add static/player-core.js static/player-core.test.js static/app.js
git commit -m "Queue tab: count the whole queue, render rows or the empty state

The summary counted only items after queueIndex, so a one-track queue read
'Your queue is empty' above a row labelled PLAYING; the empty block was also
concatenated after the rows instead of replacing them, so all three showed at
once. queueView() is pure and covered in player-core.test.js. Also 'clear' ->
'empty', one word per idea (spec 4.7)."
```

---

### Task 2: A3 — make the Now Playing header collapse actually fire

**Files:**
- Modify: `static/style.css:31` (add `--compact-header` to `:root`), `static/style.css:538` (pin the compact height), `static/style.css:884` (phone override inside the existing `@media (max-width: 860px)` block opened at line 847)
- Modify: `static/player-core.js` (append after `queueView`)
- Modify: `static/app.js:1` (import), `static/app.js:1349-1378` (comment + `updateNowHeader`)
- Test: `static/player-core.test.js` (append a new `test(...)` block)

**Interfaces:**
- Consumes: `queueView` from Task 1 is already on the `static/player-core.js` import line in `static/app.js:1` — extend that same line again, do not add a second import.
- Produces: `shouldCompactHeader({scrollTop, scrollHeight, clientHeight, headerHeight, compactHeight, compact}) -> boolean`, and the CSS custom property `--compact-header` (132px desktop, 120px at ≤860px) which is the single source of truth read by both CSS and JS.

Two corrections to the governing documents, both measured, both approved by the user:

1. **The audit's fix is wrong.** The audit says "drop `- header.offsetHeight`". Collapsing the header *grows* `#now-content`'s clientHeight (the header is a flex sibling — `index.html:210` `<div class="now-header">` vs `index.html:231` `<div id="now-content">`), which is exactly the oscillation the comment at `static/app.js:1349-1365` describes and which is real. The subtraction stays; it must subtract only the *freed* height, `headerHeight - compactHeight`.
2. **The handoff's `COMPACT_HEADER_HEIGHT = 72` is wrong.** Measured with the stylesheet loaded:

| Viewport | Panel | Expanded header | Compact header |
|---|---|---|---|
| 1440×900 | 812 | 407 (50%) | **132** (16%) |
| 390×844 | 844 | 515 (61%) | **120** (14%) |

Against the audit's measured case (`scrollHeight 636`, `clientHeight 206`, `headerHeight 546`):
- old arithmetic: `636 - 206 - 546 = -116`, needs `> 48` → never fires
- handoff's 72: freed 474 → `636 - 206 - 474 = -44` → **still never fires**
- measured 132: freed 414 → `636 - 206 - 414 = +16` → clears `> 12` but not `> 48`

So the fix needs the measured constant **and** the post-collapse threshold lowered from 48 to 12. The scroll-position thresholds stay at 48 (collapse) / 12 (expand) — that pair is what stops the boundary oscillation and is not the bug.

Pinning `height` on the compact header was verified not to clip: `.now-tabs` (40px), `#close-now` (40px) and `.large-art-wrap` (64px desktop, 56px at ≤860px per `static/style.css:884`) all stay `visibility: visible` and inside the box at both 132px and 120px. `* { box-sizing: border-box }` (`static/style.css:71`) means the 4px top padding on `static/style.css:553` is inside the pinned height.

Out of scope here, on purpose: spec §4.3's "expanded header ≤ 45% of panel height" is a separate CSS cap on the *expanded* state (currently 50% desktop / 61% phone). Spec §4.3 assigns it to the visual phase. Do not attempt it in this task — it changes `headerHeight` but not the arithmetic, and the two changes must be measurable independently.

- [ ] **Step 1: Write the failing test**

Extend the import on line 3 of `static/player-core.test.js` again, then append:

```js
import { adjacentIndex, bufferedPercent, formatTime, lyricIndex, normalizePlayerState, normalizeTrackPage, queueView, shouldCompactHeader } from "./player-core.js";
```

```js
test("the now-playing header collapses on the real measured geometry", () => {
  // Audit A3, measured at 1440x900 with synced lyrics: the header is a sibling of the
  // scroller, so subtracting its full height made `scrollable` negative and the collapse
  // could never fire. Only the height it *frees* (546 - 132) belongs in the subtraction.
  const audited = { scrollTop: 100, scrollHeight: 636, clientHeight: 206, headerHeight: 546, compactHeight: 132, compact: false };
  assert.equal(shouldCompactHeader(audited), true);
  // Same numbers under the old arithmetic: 636 - 206 - 546 = -116, which never cleared > 48.
  assert.equal(audited.scrollHeight - audited.clientHeight - audited.headerHeight, -116);
  assert.equal(shouldCompactHeader({ ...audited, compactHeight: audited.headerHeight }), false);

  // The phone header frees less, but a lyric sheet still clears it.
  assert.equal(shouldCompactHeader({ scrollTop: 100, scrollHeight: 900, clientHeight: 300, headerHeight: 515, compactHeight: 120, compact: false }), true);

  // A pane that does not overflow once collapsed must stay expanded, or it judders: collapse
  // grows clientHeight, scrollTop gets clamped to ~0, we expand, momentum collapses again.
  assert.equal(shouldCompactHeader({ scrollTop: 100, scrollHeight: 400, clientHeight: 400, headerHeight: 546, compactHeight: 132, compact: false }), false);
  assert.equal(shouldCompactHeader({ scrollTop: 100, scrollHeight: 620, clientHeight: 206, headerHeight: 546, compactHeight: 132, compact: false }), false);

  // Below the collapse threshold, however much room there is.
  assert.equal(shouldCompactHeader({ ...audited, scrollTop: 20 }), false);

  // Already compact: expand only back under 12, so the two thresholds do not meet.
  assert.equal(shouldCompactHeader({ scrollTop: 5, scrollHeight: 636, clientHeight: 620, headerHeight: 132, compactHeight: 132, compact: true }), false);
  assert.equal(shouldCompactHeader({ scrollTop: 40, scrollHeight: 636, clientHeight: 620, headerHeight: 132, compactHeight: 132, compact: true }), true);
  assert.equal(shouldCompactHeader({ scrollTop: 12, scrollHeight: 636, clientHeight: 620, headerHeight: 132, compactHeight: 132, compact: true }), true);
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test static/*.test.js`
Expected: FAIL with `SyntaxError: The requested module './player-core.js' does not provide an export named 'shouldCompactHeader'`

- [ ] **Step 3: Add `shouldCompactHeader` to `static/player-core.js`**

Append after `queueView`:

```js
// The header is a flex sibling of the scroller, so collapsing it hands its freed height to
// .now-content and shrinks the maximum scrollTop by the same amount. Subtract only what the
// collapse frees (measured: 546 -> 132 desktop, 515 -> 120 phone) and require the pane to
// still be scrollable afterwards, or a short pane oscillates. Two scroll thresholds, 48 to
// collapse and 12 to expand, so the boundary is never a single point.
export function shouldCompactHeader({ scrollTop, scrollHeight, clientHeight, headerHeight, compactHeight, compact }) {
  if (compact) return scrollTop >= 12;
  const freed = Math.max(0, headerHeight - compactHeight);
  return scrollTop > 48 && (scrollHeight - clientHeight - freed) > 12;
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `node --test static/*.test.js`
Expected: PASS (4 tests pass)

- [ ] **Step 5: Pin the compact height in CSS so JS and CSS cannot drift**

`static/style.css:31` — add the token to `:root`, after `--player-height: 88px;`:

```css
  --player-height: 88px;
  /* Read by updateNowHeader to work out how much height the collapse frees. Measured, not
     guessed: 132px holds the 40px tabs, the 40px close button and the 64px art. */
  --compact-header: 132px;
```

`static/style.css:538` — add the pinned height to the existing rule (this is the only edit to that line):

```css
.now-header.is-compact { display: grid; grid-template-areas: "art title close" "tabs tabs tabs"; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; column-gap: 14px; height: var(--compact-header); }
```

`static/style.css:884` — inside the existing `@media (max-width: 860px)` block (opened at line 847), immediately above the `.now-header.is-compact .large-art-wrap` rule:

```css
  /* The phone art is 56px, not 64px, so the collapsed header lands 12px shorter. */
  :root { --compact-header: 120px; }
  .now-header.is-compact .large-art-wrap { width: 56px; margin: 6px 0; }
```

- [ ] **Step 6: Rewrite the comment and `updateNowHeader`**

`static/app.js:1` — extend the import once more:

```js
import { adjacentIndex, bufferedPercent, formatTime, lyricIndex, normalizePlayerState, normalizeTrackPage, queueView, shouldCompactHeader } from "./player-core.js";
```

`static/app.js:1349-1378` — replace the comment and the function. The final paragraph of the old comment ("subtracting the whole header is a conservative bound") is now false and is what kept the collapse from ever firing:

```js
// Collapse the now-playing header once you scroll into the content. Two thresholds rather
// than one: a single boundary sits exactly where collapsing changes scrollHeight, so the
// header would oscillate. Collapse at 48px, expand only back under 12px.
//
// The thresholds alone are not enough. The header is a flex sibling of the scroller, so
// collapsing it hands its freed height to .now-content: clientHeight grows and the maximum
// scrollTop shrinks by the same amount. On a short pane (Details on a phone) that maximum
// drops under 12px, the browser clamps scrollTop to it, and the clamp reads as "scrolled to
// the top" -- so we expand, the pane becomes scrollable again, momentum pushes past 48 and
// it collapses once more. That feedback loop is the juddering in the Details tab.
//
// So require the pane to still be scrollable after collapsing -- but subtract only the height
// the collapse actually frees, not the whole header. Subtracting the whole header was a bound
// so conservative it was never satisfiable: measured at 1440x900 with lyrics loaded,
// 636 - 206 - 546 = -116, so the collapse could not fire at all. The collapsed header is a
// measured, CSS-pinned 132px (120px at <=860px), read from --compact-header so the two cannot
// drift; freed = 546 - 132 = 414 leaves +16px of real overflow, which clears the 12px floor.
function updateNowHeader() {
  const header = document.querySelector(".now-header");
  const content = $("now-content");
  if (!header || !content) return;
  const compact = header.classList.contains("is-compact");
  const compactHeight = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--compact-header")) || 132;
  const next = shouldCompactHeader({
    scrollTop: content.scrollTop,
    scrollHeight: content.scrollHeight,
    clientHeight: content.clientHeight,
    headerHeight: header.offsetHeight,
    compactHeight,
    compact,
  });
  if (next !== compact) setNowHeaderCompact(header, next);
}
```

`setNowHeaderCompact` and its FLIP pass (`static/app.js:1380` onwards, the `NOW_HEADER_MORPH` constant and the function through the transform loop) are untouched.

- [ ] **Step 7: Verify both suites**

Run: `node --test static/*.test.js`
Expected: PASS (4 tests pass)

Run: `./.venv/bin/python -m unittest discover -s tests`
Expected: PASS, `OK` with 63 tests (this task touches no Python)

- [ ] **Step 8: Commit**

```bash
git add static/player-core.js static/player-core.test.js static/app.js static/style.css
git commit -m "Now Playing header: collapse on the height the collapse frees

The guard subtracted the whole header from a scroll extent the header is not
part of, so at 1440x900 with lyrics it computed 636-206-546 = -116 against a
> 48 test and never fired -- the header sat at 546px of an 812px panel. Subtract
only headerHeight - compactHeight and floor the remainder at 12. compactHeight
is measured (132px, 120px on phones) and pinned in CSS as --compact-header,
which JS reads, so the constant cannot drift from the layout.

Keeping the subtraction matters: dropping it entirely, as the audit suggested,
reintroduces the Details-tab oscillation the two thresholds exist to prevent."
```

---

### Task 3: library rows always render as un-liked

**Files:**
- Modify: `core.py:952-958` (the `SELECT` list in `Database.list_tracks`)
- Test: `tests/test_core.py` (new method on the existing `CoreTests` class — the file exists, extend it)

**Interfaces:**
- Consumes: nothing (independent of Tasks 1 and 2; can be done in any order)
- Produces: nothing new. `Database.list_tracks(...)["items"][i]["liked"]` starts returning the truth. `Database.set_liked(key: str, liked: bool) -> dict` and `Database.track_summaries(keys: Iterable[str]) -> list[dict]` are unchanged and already correct.

Not in the audit — found by measurement. `Database._track_summary` (`core.py:886-907`) shapes every row, and `core.py:900` is:

```python
"liked": value.get("liked_at") is not None,
```

`list_tracks` selects `t.rowid`, `t.chat_id`, `t.message_id`, `t.file_name`, `t.file_size`, `t.duration_ms`, `t.telegram_title`, `t.telegram_artist`, `t.sent_at`, `t.document_id`, `s.title`, `s.kind`, `s.selected`, `o.payload` — and **not** `t.liked_at`. The `.get` returns `None`, so `liked` is unconditionally `False` for every library row. `track_summaries` does select it (`core.py:1042`), which is why the queue shows hearts correctly and the library never does. Measured directly:

```
list_tracks liked -> False     (wrong)
summaries  liked -> True       (correct)
liked page count  -> 1         (the like did persist)
```

The like is stored and `liked=True` filtering works (`core.py:924`, `clauses.append("t.liked_at IS NOT NULL")`) — only the rendered heart lies. Consequence for the redesign: any Phase-1 like-affordance work is invisible in All music until this lands, and always looked fine in Liked songs.

Do **not** touch the `COUNT(*)` query at `core.py:975-985`: it counts rows, it does not shape them, and adding a column there would only slow it. The `ponytail:` comment at `core.py:967-970` explaining why the count is a separate query stays as-is.

- [ ] **Step 1: Write the failing test**

Append to `class CoreTests` in `tests/test_core.py`. No new imports are needed — `Database`, `Path` and `tempfile` are already imported at the top.

```python
    def test_library_rows_report_liked_the_same_as_queue_rows(self):
        # list_tracks omitted t.liked_at from its SELECT, so _track_summary's
        # value.get("liked_at") was always None and every library row rendered un-liked --
        # while the queue, which goes through track_summaries, showed the heart correctly.
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            database.upsert_tracks([{"chatId": "1", "messageId": "2", "fileName": "song.mp3",
                                     "mimeType": "audio/mpeg", "title": "T", "artist": "A"}])
            database.set_liked("1:2", True)

            self.assertIs(True, database.list_tracks()["items"][0]["liked"])
            # Both read paths, asserted together, so they cannot diverge again.
            self.assertIs(True, database.track_summaries(["1:2"])[0]["liked"])
            # And unliking has to come back through the same path.
            database.set_liked("1:2", False)
            self.assertIs(False, database.list_tracks()["items"][0]["liked"])
            self.assertIs(False, database.track_summaries(["1:2"])[0]["liked"])
            database.close()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./.venv/bin/python -m unittest tests.test_core.CoreTests.test_library_rows_report_liked_the_same_as_queue_rows -v`
Expected: FAIL with `AssertionError: False is not True` on the first `assertIs`

- [ ] **Step 3: Add `t.liked_at` to the SELECT in `list_tracks`**

`core.py:952-958` — one column added to the row-shaping query:

```python
        rows = reader.execute(
            f"""
            SELECT t.rowid AS track_rowid, t.chat_id, t.message_id, t.file_name,
                   t.file_size, t.duration_ms, t.telegram_title, t.telegram_artist,
                   t.sent_at, t.document_id, t.liked_at, s.title AS source_title,
                   s.kind AS source_kind, s.selected AS source_selected,
                   o.payload AS override_payload
            FROM tracks t
            JOIN sources s ON s.chat_id = t.chat_id
            LEFT JOIN metadata_overrides o
                ON o.chat_id = t.chat_id AND o.message_id = t.message_id
            WHERE {where}
            ORDER BY t.sent_at DESC, t.rowid DESC
            LIMIT ? OFFSET ?
            """,
            (*parameters, limit, offset),
        ).fetchall()
```

The `COUNT(*)` block below it (`core.py:975-985`) is left exactly as it is.

- [ ] **Step 4: Run the test to verify it passes**

Run: `./.venv/bin/python -m unittest tests.test_core.CoreTests.test_library_rows_report_liked_the_same_as_queue_rows -v`
Expected: PASS, `OK` with 1 test

- [ ] **Step 5: Verify the full suites**

Run: `./.venv/bin/python -m unittest discover -s tests`
Expected: PASS, `OK` with 64 tests (63 before this task, plus the new one)

Run: `node --test static/*.test.js`
Expected: PASS (unchanged by this task)

- [ ] **Step 6: Commit**

```bash
git add core.py tests/test_core.py
git commit -m "Library rows report the real liked state

list_tracks' SELECT omitted t.liked_at, so _track_summary's
value.get(\"liked_at\") was always None and every row in All music and every
per-source list rendered un-liked. track_summaries did select it, which is why
the queue looked right and the library did not, and why 'Liked songs' filtered
correctly all along -- only the rendered heart lied. The test asserts both read
paths at once so they cannot drift apart again. The separate COUNT(*) query is
untouched; it counts rows, it does not shape them."
```

---

### Task 4: rail row restructure — folds A4 + A5 + D1 + D11 + E3

**Files:**
- Create: `static/format.js`, `static/format.test.js`
- Modify: `static/app.js:551-556` (source row template), `static/app.js:553` `:562` `:796` `:1183` (the four raw-`kind` sites), `static/app.js:1242-1250` (`unselectSources`)
- Modify: `static/style.css:947` and `static/style.css:358` (both `.source-link` declarations), `static/style.css:963-977` (`.source-actions` / `.source-count`), `static/style.css:1243`
- Modify: `static/index.html:142` (bulk button label)
- Test: `static/format.test.js`

**Interfaces:**
- Consumes: nothing
- Produces: `sourceKindLabel(kind: string) -> string` from the new `static/format.js`. Task 11 adds `errorCopy(error)` and Task 16 adds `formatPostedDate(seconds, nowSeconds)` to this **same module** — do not create a second formatting file.

Five findings collapse into one row rewrite, because they are all the same mistake: real content hidden behind hover.

- **A4 — the count is visible in no state.** `.source-count` sits inside `<span class="source-actions">` (`app.js:555`), which is `position: absolute; opacity: 0` (`style.css:963-973`). On hover the wrapper becomes visible, but `style.css:977` then sets `.source-link:hover .source-count { display: none }`. Probed at rest: text `"412"`, `display: block`, wrapper `opacity: 0`. Meanwhile "All music 811" and "Liked songs 27" above it *do* show counts, so the column reads as broken rather than absent. `style.css:1243` hides it permanently on coarse pointers too.
- **A5 — the hover overlay occludes the row you are pointing at.** `.source-actions` has `background: var(--surface)`, so on hover it paints an opaque panel over the right of the row, clipping the title mid-word — observed as "Hyperdul", "ambient d…", "Nyege Nyeg⌐". The comment at `style.css:959-962` explains the actions were moved out of the grid because they squeezed titles to ~46px; the overlay fixed the reflow and reintroduced the occlusion.
- **D1 — `source.kind` reaches the DOM as a raw database enum, in four places** (the audit found three; the fourth is `app.js:1183`). `bot · needs attention` also concatenates an error state into what reads as a category.
- **D11 — three unlabelled 27px icons**, two of them near-identical sync variants, the third a repeat-loop glyph that reads as *repeat*, not *rescan*.
- **E3 — the bulk verb lies.** `index.html:142` says "Unselect"; the handler POSTs `/api/sources/bulk-select` with `false`, which removes sources from the library.

- [ ] **Step 1: Write the failing test**

Create `static/format.test.js`:

```js
import test from "node:test";
import assert from "node:assert/strict";
import { sourceKindLabel } from "./format.js";

test("source kinds render as human labels, never raw enums", () => {
  // The four kinds telegram_service.classify_entity can produce (telegram_service.py:602).
  assert.equal(sourceKindLabel("channel"), "Channel");
  assert.equal(sourceKindLabel("private"), "Private chat");
  assert.equal(sourceKindLabel("saved"), "Saved Messages");
  assert.equal(sourceKindLabel("bot"), "Bot");

  // A kind we have never seen must still not put a database value on screen. Telegram adds
  // entity types; the rail should degrade to a generic noun rather than print "megagroup".
  assert.equal(sourceKindLabel("megagroup"), "Chat");
  assert.equal(sourceKindLabel(""), "Chat");
  assert.equal(sourceKindLabel(undefined), "Chat");
  assert.equal(sourceKindLabel(null), "Chat");
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test static/*.test.js`
Expected: FAIL with `Cannot find module '/home/kinofare/Projects/telegram-music-player/static/format.js'`

- [ ] **Step 3: Create `static/format.js`**

```js
// Every human-facing string that is derived from stored data is formatted here, so the rail,
// the library header, global search and the discover dialog cannot disagree about what a
// "saved" chat is called. Pure and DOM-free, so it is testable under node --test.
const KIND_LABELS = { channel: "Channel", private: "Private chat", saved: "Saved Messages", bot: "Bot" };

export function sourceKindLabel(kind) {
  return KIND_LABELS[kind] || "Chat";
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `node --test static/*.test.js`
Expected: PASS

- [ ] **Step 5: Route all four raw-`kind` sites through the formatter**

`static/app.js:1` — add the import beside the existing `player-core.js` one:

```js
import { sourceKindLabel } from "./format.js";
```

`static/app.js:553` — the rail `<small>`. The sync error leaves the kind string and becomes its own mark, so a failing sync stops reading as a category:

```js
    <span class="source-copy"><strong>${source.pinnedAt ? `<span class="source-pin-mark" aria-hidden="true">${icon("pin")}</span>` : ""}${escapeHtml(source.title)}</strong><small>${escapeHtml(sourceKindLabel(source.kind))}${source.syncError ? `<span class="source-error-dot" role="img" aria-label="Sync problem: ${escapeAttr(source.syncError)}" title="${escapeAttr(source.syncError)}"></span>` : ""}</small></span>
```

`static/app.js:562` — the library header eyebrow:

```js
  $("source-kind").textContent = state.likedMode ? "Saved locally" : selected ? (selected.temporary ? "Temporary source" : sourceKindLabel(selected.kind)) : "Your Telegram";
```

`static/app.js:796` — global search results, replacing `${escapeHtml(source.kind)}`:

```js
<small>${escapeHtml(sourceKindLabel(source.kind))}${source.trackCount ? ` · ${source.trackCount.toLocaleString()} known tracks` : ""}</small>
```

`static/app.js:1183` — the discover dialog, replacing `${escapeHtml(item.kind)}`:

```js
<small>${escapeHtml(sourceKindLabel(item.kind))} · ${item.musicFileCount ?? item.trackCount ?? "…"} music files</small>
```

- [ ] **Step 6: Make the count a real column and collapse the icons into one menu**

`static/app.js:555` — replace the `.source-actions` span. The count becomes a grid sibling of the title, and the three icon buttons become one always-rendered `⋯` reusing `openMenu`:

```js
    <span class="source-count">${source.trackCount.toLocaleString()}</span>
    <button class="icon-button source-menu" type="button" data-source-menu="${source.chatId}" aria-label="Actions for ${escapeHtml(source.title)}">${icon("more")}</button>
```

**Extend the existing delegated listener at `static/app.js:1907`** — do not add a second `document`-level click handler. That line already routes `[data-sync-source]`, `[data-pin-source]`, `[data-bulk-source]`, `[data-temporary-source]` and `[data-source]`; the first two branches are replaced by one menu branch, which must come **before** the `[data-source]` branch or clicking the menu also selects the source:

```js
$("source-list").addEventListener("click", (event) => {
  const menu = event.target.closest("[data-source-menu]");
  if (menu) {
    const source = state.sources.find((item) => item.chatId === menu.dataset.sourceMenu);
    if (!source) return;
    const box = menu.getBoundingClientRect();
    // Labels, not glyphs: the old rescan button drew a repeat loop, which reads as "repeat".
    // pinSource(chatId) takes one argument and toggles from source.pinnedAt itself.
    return openMenu([
      { label: "Sync new tracks", action: () => syncSource(source.chatId, false) },
      { label: "Full rescan", action: () => syncSource(source.chatId, true) },
      { label: source.pinnedAt ? "Unpin from top" : "Pin to top", action: () => pinSource(source.chatId) },
    ], box.left, box.bottom + 4);
  }
  const checkbox = event.target.closest("[data-bulk-source]");
  if (checkbox) { checkbox.checked ? state.selectedSources.add(checkbox.dataset.bulkSource) : state.selectedSources.delete(checkbox.dataset.bulkSource); return renderSources(); }
  const temporary = event.target.closest("[data-temporary-source]");
  if (temporary && !state.bulk) return selectTemporary();
  const row = event.target.closest("[data-source]");
  if (row && !state.bulk) selectSource(row.dataset.source);
});
```

Verified signatures being reused: `syncSource(chatId, full = false)` at `app.js:1147`, `pinSource(chatId)` at `app.js:1196` (it derives `pinned = !source.pinnedAt` internally — passing a second argument would be ignored), `openMenu(actions, x, y)` at `app.js:1411` whose click dispatch at `app.js:2091` reads `menu._actions[index].action`, so the action shape is unchanged.

**D10 — bulk mode must suppress the menu.** The audit found hover actions still active during bulk-select, so you could pin while selecting. The template already swaps the avatar for a checkbox when `state.bulk`; the menu button must be omitted on the same condition, so wrap it: `${state.bulk ? "" : `<button class="icon-button source-menu" …>`}`. Spec §4.2 also moves "All music" and "Liked songs" out of the selectable list in bulk mode.

`static/style.css:947` **and** `static/style.css:358` — both `.source-link` declarations get the count and menu as real columns. Fixing one copy fixes nothing:

```css
.source-link { position: relative; grid-template-columns: 34px minmax(0, 1fr) auto 30px; gap: 10px; min-height: 54px; transition: color var(--dur-1) var(--ease-out), background-color var(--dur-1) var(--ease-out), border-color var(--dur-1) var(--ease-out), opacity var(--dur-1) var(--ease-out), box-shadow var(--dur-1) var(--ease-out); }
```

`static/style.css:963-977` — delete the whole `.source-actions` block (it has no children left), delete `.source-link:hover .source-count { display: none }`, and give the menu the always-visible treatment spec §4.1 requires. Both of this task's hidden-content bugs came from `opacity: 0`, so nothing real hides behind hover again:

```css
.source-count { color: var(--graphite); font-family: var(--font-mono); font-size: var(--text-micro); font-variant-numeric: tabular-nums; }
/* Always rendered, never opacity: 0. The count used to live inside an invisible overlay that
   also painted over the title on hover -- two bugs from one piece of hidden content. */
.source-menu { width: 30px; height: 30px; color: var(--graphite); }
.source-link:hover .source-menu, .source-link:focus-within .source-menu { color: var(--ink); }
.source-error-dot { display: inline-block; width: 6px; height: 6px; margin-left: 6px; border-radius: 50%; background: var(--danger); vertical-align: 1px; }
```

`static/style.css:1243` — delete `.source-link .source-count { display: none; }` so the count survives on a phone (spec §4.5 keeps the count and drops the decorative eyebrow instead).

- [ ] **Step 7: Rename the bulk verb in all three places**

`static/index.html:142`:

```html
<button id="bulk-unselect" class="text-button danger" type="button">Remove from library</button>
```

`static/app.js:1242-1250` — the confirm and the toast must use the same verb as the button, so the name persists through the flow (spec §4.7):

```js
async function unselectSources(ids) {
  if (!ids.length || !await confirmAction("Remove from library?", "They’ll disappear from this player and stop syncing. Nothing will be deleted or left in Telegram.", "Remove from library")) return;
  try {
    await api("/api/sources/bulk-select", { method: "POST", body: JSON.stringify({ chatIds: ids, selected: false }) });
    state.selectedSources.clear(); state.bulk = false; $("bulk-bar").hidden = true; state.libraryCache.clear();
    if (ids.includes(state.source)) state.source = "";
    await loadLibrary(true); toast(ids.length === 1 ? "Source removed from library" : `${ids.length} sources removed from library`);
  } catch (error) { showError(error, () => unselectSources(ids)); }
}
```

- [ ] **Step 8: Verify**

Run: `node --test static/*.test.js`
Expected: PASS

Run: `./.venv/bin/python -m unittest discover -s tests`
Expected: PASS, `OK` (unchanged by this task)

Then check by hand that the count is visible at rest and on hover, and that hovering no longer clips the title. `--text-micro` and `--graphite` do not exist until Task 13; until then the count renders unstyled-but-visible, which is expected and is why this task's check is "visible in every state", not "visible at 11px".

- [ ] **Step 9: Commit**

```bash
git add static/format.js static/format.test.js static/app.js static/style.css static/index.html
git commit -m "Rail rows: show the count, stop occluding titles, label the actions

The per-source count lived inside an opacity: 0 overlay that a :hover rule then
display: none'd, so it rendered in no state at all -- while All music and Liked
songs showed theirs, making the column look broken. The same overlay painted
var(--surface) over the right of the row on hover and clipped titles mid-word.
Both bugs were one piece of real content hidden behind hover, so the count is
now a grid column and the actions are always rendered.

Three unlabelled 27px icons (two near-identical sync variants, one repeat glyph
that reads as 'repeat' not 'rescan') collapse into one labelled menu. source.kind
reached the DOM raw in four places; all four now go through sourceKindLabel, and
the sync error is its own mark rather than a suffix on the category. Bulk
'Unselect' POSTs selected: false, which removes sources -- so it says so."
```

---

### Task 5: A6 — the collapsed rail grows a scrollbar from a 1px overflow

**Files:**
- Modify: `static/style.css:1001`
- Test: deferred to `tests/test_layout.py` (Task 8) — see the honest note below

**Interfaces:**
- Consumes: nothing
- Produces: nothing

The arithmetic, verified in the stylesheet: the collapsed rail is a `68px` grid column (`style.css:988`, `.app-shell.sidebar-collapsed { grid-template-columns: 68px minmax(0, 1fr) }`) with `padding-inline: 11px` (`style.css:990`), giving a 46px content box. `style.css:1001` then sets `.sidebar-collapsed .source-link { width: 46px }` — exactly the full width, leaving nothing for the 1px vertical scrollbar. Result per the audit: `nav.clientWidth = 45`, `nav.scrollWidth = 46`, and Chromium renders a horizontal scrollbar **with arrow buttons**, which appears as a stray `◀ ● ▶` control floating in the rail.

**Honest note on verification.** My bare-DOM probe did **not** reproduce the overflow — it measured `clientWidth 237 / scrollWidth 237`, because reproducing it needs seeded source rows plus `.sidebar-collapsed` on the correct ancestor (`.app-shell`, not `body`). So this task ships the fix on the strength of the stylesheet arithmetic, and its assertion is added to `tests/test_layout.py` once Task 8 exists. Do not claim it is verified before that assertion runs green.

- [ ] **Step 1: Apply the fix**

`static/style.css:1001` — `width: 100%` lets the row shrink by the scrollbar's width instead of overflowing, and `max-width` keeps the intended size when there is no scrollbar:

```css
.sidebar-collapsed .source-link { width: 100%; max-width: 46px; min-height: 46px; grid-template-columns: 1fr; place-items: center; padding: 0; border-left: 0; border-bottom-color: transparent; border-radius: var(--radius-control); }
```

Note `style.css:996` already lists `.sidebar-collapsed .source-count` among the elements hidden when collapsed — Task 4 makes the count a real grid column, so confirm that entry is still present after Task 4 lands, or collapsed rows will try to render a count in a 46px avatar-only cell.

- [ ] **Step 2: Check by hand until Task 8 exists**

With the rail collapsed and enough sources to overflow vertically, in the devtools console:

```js
const nav = document.querySelector(".source-rail nav") || document.getElementById("source-list").closest("nav");
({ clientWidth: nav.clientWidth, scrollWidth: nav.scrollWidth, overflowX: nav.scrollWidth - nav.clientWidth });
```

Expected after the fix: `overflowX: 0`, and no horizontal scrollbar or arrow buttons in the rail.

- [ ] **Step 3: Add the standing assertion once Task 8 exists**

In `tests/test_layout.py`, with sources seeded and the rail collapsed:

```python
    def test_collapsed_rail_does_not_overflow_horizontally(self):
        page = self.page(1440, 900)
        page.evaluate("() => document.querySelector('.app-shell').classList.add('sidebar-collapsed')")
        page.wait_for_timeout(120)
        client, scroll = page.evaluate("""() => {
          const nav = document.getElementById('source-list').closest('nav');
          return [nav.clientWidth, nav.scrollWidth];
        }""")
        self.assertEqual(client, scroll, "collapsed rail overflows horizontally, which spawns a scrollbar with arrows")
```

- [ ] **Step 4: Verify**

Run: `./.venv/bin/python -m unittest discover -s tests`
Expected: PASS, `OK`

- [ ] **Step 5: Commit**

```bash
git add static/style.css
git commit -m "Collapsed rail: stop overflowing by 1px

The rail column is 68px with padding-inline: 11px, so the content box is exactly
46px -- and .source-link was width: 46px, leaving nothing for the 1px vertical
scrollbar. clientWidth 45 vs scrollWidth 46 made Chromium draw a horizontal
scrollbar with arrow buttons, which reads as a stray control floating in the
rail. width: 100% with max-width: 46px lets the row give the scrollbar its pixel."
```

---

### Task 6: A7 — every button in the metadata dialog is 55% too tall

**Files:**
- Modify: `static/style.css:790` (or `:1172`), `static/style.css:791`
- Test: manual computed-style probe below; the standing assertion moves into `tests/test_layout.py` after Task 8

**Interfaces:**
- Consumes: nothing
- Produces: nothing

A specificity collision, both rules verified in place:

- `style.css:790` — `.metadata-form label { display: grid; gap: 8px; }` — specificity **(0,1,1)**
- `style.css:1172` — `.inline-choice { display: inline-flex; align-items: center; gap: 7px; font-size: 11px; }` — specificity **(0,1,0)**

The type-and-class selector wins, so the "Cover" label (`index.html:327`, `<label class="inline-choice">Cover <select id="cover-quality">…`) stacks its text above the select instead of sitting beside it, making that block 62px tall. Then `style.css:791` — `.form-actions { display: flex; flex-wrap: wrap; gap: 8px 12px; }` — has `align-items: normal`, which computes to `stretch`, so Save changes / Reset to Telegram / Fetch metadata all stretch from `min-height: 40px` to **62px**. Measured: `inlineChoiceDisplay: "grid"`, all three buttons 62px.

**`.form-actions` is used in seven places** — `index.html:323` (as `form-actions full-row`), `:345`, `:392`, `:427`, `:438`, plus the error and confirm dialogs at `:467` and `:468`. Adding `align-items: center` is therefore a global change. That is intended (a row of buttons should never stretch to its tallest sibling), but check the two alert dialogs and the settings rows still sit correctly.

- [ ] **Step 1: Probe the current state to confirm the bug**

In the devtools console with the metadata dialog open:

```js
const choice = document.querySelector(".metadata-form .inline-choice");
const buttons = [...document.querySelectorAll("#metadata-form .form-actions .button")];
({ inlineChoiceDisplay: getComputedStyle(choice).display,
   choiceHeight: Math.round(choice.getBoundingClientRect().height),
   buttonHeights: buttons.map((b) => Math.round(b.getBoundingClientRect().height)) });
```

Expected before the fix: `display: "grid"`, `choiceHeight: 62`, `buttonHeights: [62, 62, 62]`.

- [ ] **Step 2: Scope the label rule so `.inline-choice` wins**

Two ways to break the collision; take the `:where()` form, because it fixes the cause once rather than adding a more specific selector for every widget that ever sits inside a metadata label. `:where()` contributes **zero** specificity, so `.metadata-form label` drops to (0,1,0) and loses to nothing else it currently beats:

`static/style.css:790`:

```css
:where(.metadata-form) label { display: grid; gap: 8px; }
```

`static/style.css:791` — buttons in a row size to their own content, not to the tallest sibling:

```css
.form-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; }
```

- [ ] **Step 3: Re-run the probe**

Expected after the fix: `display: "inline-flex"`, `choiceHeight: 40`, `buttonHeights: [40, 40, 40]`.

- [ ] **Step 4: Add the standing assertion once Task 8 exists**

```python
    def test_metadata_dialog_buttons_are_not_stretched(self):
        page = self.page(1440, 900)
        page.evaluate("() => document.getElementById('metadata-dialog').showModal()")
        page.wait_for_timeout(120)
        display, heights = page.evaluate("""() => [
          getComputedStyle(document.querySelector('.metadata-form .inline-choice')).display,
          [...document.querySelectorAll('#metadata-form .form-actions .button')].map((b) => Math.round(b.getBoundingClientRect().height)),
        ]""")
        self.assertEqual("inline-flex", display)
        self.assertTrue(all(h == 40 for h in heights), f"buttons stretched to {heights} instead of 40px")
```

- [ ] **Step 5: Verify**

Run: `./.venv/bin/python -m unittest discover -s tests`
Expected: PASS, `OK`

- [ ] **Step 6: Commit**

```bash
git add static/style.css
git commit -m "Metadata dialog: stop stretching every button to 62px

.metadata-form label at (0,1,1) beat .inline-choice at (0,1,0), so the Cover
label stacked its text above the select and became 62px; .form-actions is a flex
row with align-items: normal (= stretch), so all three buttons grew from their
40px min-height to match it. :where() drops the label rule to zero specificity
so the more specific intent wins, and button rows now size to their own content.

align-items: center lands on all seven .form-actions uses, which is the point --
a row of buttons should never stretch to its tallest sibling."
```

---

### Task 7: A8 — a failed metadata lookup leaves a skeleton shimmering forever

**Files:**
- Modify: `static/app.js:1275-1285` (`fetchMetadata`), `static/app.js:1264` (`saveMetadata`), `static/app.js:1273` (`resetMetadata`), `static/app.js:1294` (`applyCandidate`)
- Test: manual probe below (the failure path needs a stubbed 500; the standing assertion lands with Task 8's harness)

**Interfaces:**
- Consumes: `errorCopy(error) -> string` from `static/format.js` — **defined in Task 11**. If Task 11 has not landed yet, use `error.message` here and change the four call sites in Task 11; do not write a second copy of the fallback logic.
- Produces: nothing

`fetchMetadata` (`app.js:1275-1285`) sets `$("candidate-section").hidden = false` and fills `#candidate-list` with `'<div class="list-skeleton"><span></span><span></span></div>'`, then:

```js
} catch (error) { $("metadata-status").textContent = error.message; }
```

The catch never clears the list or re-hides the section. On any failure — and MusicBrainz rate-limits aggressively — the user sees a loading animation and an error message simultaneously, indefinitely. The `finally` that re-enables the button and removes `aria-busy` is correct and stays.

**Root cause, not symptom.** The same bare catch appears in all four metadata handlers, verified: `saveMetadata` (`app.js:1264`), `resetMetadata` (`app.js:1273`), `fetchMetadata` (`app.js:1285`), `applyCandidate` (`app.js:1294`). Patching only the path the audit named leaves three siblings leaking raw `error.message` into `#metadata-status` — including client-side `TypeError`s. One shared helper fixes all four, which is also the smaller diff.

- [ ] **Step 1: Probe the current behaviour**

With the metadata dialog open, force the lookup to fail and watch both elements at once:

```js
const originalFetch = window.fetch;
window.fetch = (url, options) => String(url).includes("/metadata/search")
  ? Promise.resolve(new Response('{"error":{"message":"Rate limited"}}', { status: 429 }))
  : originalFetch(url, options);
document.getElementById("fetch-metadata").click();
// after it settles:
setTimeout(() => console.log({
  sectionHidden: document.getElementById("candidate-section").hidden,
  listHtml: document.getElementById("candidate-list").innerHTML.slice(0, 40),
  status: document.getElementById("metadata-status").textContent,
}), 500);
window.fetch = originalFetch;
```

Expected before the fix: `sectionHidden: false`, `listHtml` still containing `list-skeleton`, and a status message — the skeleton and the error on screen together.

- [ ] **Step 2: Add one shared failure handler and use it in all four catches**

Add beside the metadata handlers in `static/app.js`:

```js
// All four metadata handlers used to funnel error.message straight into the status line, and
// fetchMetadata additionally left its skeleton and section up, so a rate-limited lookup showed
// a loading animation and an error at the same time, forever.
function metadataFailed(error) {
  $("candidate-list").innerHTML = "";
  $("candidate-section").hidden = true;
  $("metadata-status").textContent = errorCopy(error);
}
```

Then replace the catch in each of the four, leaving every other line untouched:

```js
  } catch (error) { metadataFailed(error); }
```

`fetchMetadata`'s `finally` stays exactly as it is:

```js
  finally { button.disabled = false; button.removeAttribute("aria-busy"); }
```

- [ ] **Step 3: Re-run the probe**

Expected after the fix: `sectionHidden: true`, `listHtml: ""`, and a written message rather than raw exception text.

- [ ] **Step 4: Add the standing assertion once Task 8 exists**

```python
    def test_failed_metadata_lookup_clears_its_skeleton(self):
        page = self.page(1440, 900)
        page.route("**/metadata/search", lambda route: route.fulfill(
            status=429, content_type="application/json",
            body='{"error": {"message": "Rate limited", "retryable": true}}'))
        page.evaluate("() => document.getElementById('metadata-dialog').showModal()")
        page.click("#fetch-metadata")
        page.wait_for_function("() => !document.getElementById('fetch-metadata').disabled")
        hidden, markup = page.evaluate("""() => [
          document.getElementById('candidate-section').hidden,
          document.getElementById('candidate-list').innerHTML,
        ]""")
        self.assertTrue(hidden, "candidate section stayed open after a failed lookup")
        self.assertNotIn("list-skeleton", markup, "the loading skeleton outlived the failure")
```

- [ ] **Step 5: Verify**

Run: `node --test static/*.test.js`
Expected: PASS

Run: `./.venv/bin/python -m unittest discover -s tests`
Expected: PASS, `OK`

- [ ] **Step 6: Commit**

```bash
git add static/app.js
git commit -m "Metadata dialog: clear the skeleton when a lookup fails

fetchMetadata showed a skeleton, then its catch only wrote the status line -- so
a rate-limited MusicBrainz lookup left a loading animation and an error message
on screen together, indefinitely. The same bare catch was in all four metadata
handlers, so one shared metadataFailed() fixes the leak everywhere rather than
patching the single path the audit happened to name."
```

---

### Task 8: `tests/test_layout.py` — the Playwright harness, and A1

**Files:**
- Create: `tests/test_layout.py`
- Modify: `static/style.css` — the `.now-panel` rule in **both** 860px blocks (~872-886 **and** 1222)
- Test: `tests/test_layout.py` itself

**Interfaces:**
- Consumes: nothing
- Produces: `LayoutTests` with helpers `self.page(width, height)` (a stubbed, seeded page at that viewport) and `self.open_now_panel(page)`. **Tasks 5, 6, 7, 9, 10 and 24 all add assertions to this class** — their deferred checks become real here.

This is the keystone task: it is what stops the redesign making unmeasured visual claims. Three of the audit's first-pass findings were wrong because a *stub* was wrong, so the harness's stubs must be checked against their real producers, not copied.

**A1, the bug it exists to prove.** Measured at 390×844 with the panel open: `#now-panel` is `position: fixed`, `top/bottom: 0px/0px`, `z-index: 25`, rect 24..868; `#player` is `position: relative`, `z-index: 10`, rect 768..844. So the panel covers the transport, `document.elementFromPoint()` at the play button returns `SECTION#now-panel`, and the button is not hittable. The panel contains **no transport controls at all** — only close, the three tabs, and the lyrics/metadata editors. Tap the artwork and you cannot pause, skip, seek, or see progress; you must find the small × to regain control. This is the most-used screen on mobile.

Spec §4.5's fix is structural rather than additive: stop the panel above the player instead of adding a second transport that has to stay in sync.

- [ ] **Step 1: Write the harness and the failing A1 test**

Create `tests/test_layout.py`:

```python
import json
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - the suite must still run without a browser
    sync_playwright = None

ROOT = Path(__file__).resolve().parent.parent

SOURCES = [
    {"chatId": "-1001", "kind": "channel", "title": "Hyperdub", "username": "hyperdub", "selected": True,
     "sortOrder": 0, "lastPostAt": 1753800000, "trackCount": 412, "lastMessageId": 9001,
     "lastSyncedAt": 1753830000, "syncError": None, "pinnedAt": 1753000000},
    {"chatId": "-1003", "kind": "private", "title": "ambient dumps", "username": None, "selected": True,
     "sortOrder": 1, "lastPostAt": 1753600000, "trackCount": 96, "lastMessageId": 220,
     "lastSyncedAt": 1753810000, "syncError": None, "pinnedAt": None},
    {"chatId": "-1004", "kind": "saved", "title": "Saved Messages", "username": None, "selected": True,
     "sortOrder": 2, "lastPostAt": 1753500000, "trackCount": 54, "lastMessageId": 800,
     "lastSyncedAt": 1753700000, "syncError": None, "pinnedAt": None},
    {"chatId": "-1005", "kind": "bot", "title": "@deepcuts_bot", "username": "deepcuts_bot", "selected": True,
     "sortOrder": 3, "lastPostAt": 1753400000, "trackCount": 31, "lastMessageId": 90,
     "lastSyncedAt": 1753600000, "syncError": "Flood wait: retry in 42s", "pinnedAt": None},
    {"chatId": "-1006", "kind": "channel", "title": "Nyege Nyege Tapes", "username": "nyegenyege", "selected": True,
     "sortOrder": 4, "lastPostAt": 1753300000, "trackCount": 0, "lastMessageId": 0,
     "lastSyncedAt": None, "syncError": None, "pinnedAt": None},
]

TITLES = [
    ("Angels", "Burial", "-1001", 391000),
    ("Rival Dealer", "Burial", "-1001", 602000),
    ("Sines", "Kode9 & The Spaceape", "-1001", 258000),
    ("An Empty Bliss Beyond This World", "The Caretaker", "-1003", 225000),
    ("03 - untitled draft mixdown FINAL v2 (do not distribute).mp3", "Unknown artist", "-1004", 764000),
    ("Kabuubi", "Otim Alpha", "-1006", 245000),
]


def _tracks(count=48):
    items = []
    for index in range(count):
        title, artist, chat_id, duration = TITLES[index % len(TITLES)]
        source = next(item for item in SOURCES if item["chatId"] == chat_id)
        items.append({
            "key": f"{chat_id}:{1000 + index}",
            "title": title if index < len(TITLES) else f"{title} (take {index // len(TITLES) + 1})",
            "artist": artist,
            "durationMs": duration,
            "sentAt": 1753800000 - index * 3600,
            "artworkVersion": f"v{index}",
            "liked": index % 5 == 1,
            "source": {"chatId": chat_id, "title": source["title"], "kind": source["kind"], "selected": True},
        })
    return items


class Handler(SimpleHTTPRequestHandler):
    # index.html asks for /assets/style.css because app.py:299 mounts /assets -> static/.
    # Without this rewrite every stylesheet 404s and every geometry assertion measures
    # unstyled markup, which is how you get a green test that proves nothing.
    def translate_path(self, path):
        if path.startswith("/assets/"):
            path = path[len("/assets"):]
        return super().translate_path(path)

    def log_message(self, *args):
        pass


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class LayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(ROOT / "static")))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()

    def _stub(self, route):
        # urlsplit rather than hand-rolled splitting: track keys contain ':' and paths carry
        # query strings, and both break naive parsing.
        path = urlsplit(route.request().url).path
        if path.endswith("/cover") or path.endswith("/avatar"):
            # A 404 here is not neutral: a missing cover triggers the img error handler, which
            # replaces the element and changes the geometry being measured.
            return route.fulfill(status=200, content_type="image/svg+xml", body=(
                '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300">'
                '<rect width="300" height="300" fill="#8c2f24"/></svg>'))
        if path.endswith("/audio"):
            return route.fulfill(status=200, content_type="audio/wav", body=b"")
        if path == "/api/auth/status":
            return route.fulfill(status=200, content_type="application/json",
                                 body='{"passwordEnabled": true, "authenticated": true}')
        if path == "/api/status":
            return route.fulfill(status=200, content_type="application/json",
                                 body='{"unlocked": true, "telegram": {"linked": true, "userId": 777, "displayName": "test"}, "startupError": null}')
        if path == "/api/sources":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(SOURCES))
        if path == "/api/library/stats":
            return route.fulfill(status=200, content_type="application/json", body='{"likedCount": 27}')
        if path == "/api/cache/status":
            # "files", not "count". seed.js gets this wrong and it produced a retracted finding.
            return route.fulfill(status=200, content_type="application/json",
                                 body='{"bytes": 184320000, "files": 41, "states": {}}')
        if path == "/api/tracks":
            items = _tracks()
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"items": items, "offset": 0, "total": len(items)}))
        if path == "/api/settings":
            return route.fulfill(status=200, content_type="application/json",
                                 body='{"musicbrainzContact": "", "coverQuality": "1200", "prefetchCount": 1, "bindHost": "127.0.0.1"}')
        if path == "/api/network":
            return route.fulfill(status=200, content_type="application/json",
                                 body='{"bindHost": "127.0.0.1", "activeHost": "127.0.0.1", "managed": false, "inDocker": false}')
        return route.fulfill(status=200, content_type="application/json", body="{}")

    def page(self, width, height):
        page = self.browser.new_page(viewport={"width": width, "height": height})
        page.route("**/api/**", self._stub)
        page.goto(f"http://127.0.0.1:{self.port}/index.html", wait_until="load")
        page.wait_for_timeout(200)
        self.addCleanup(page.close)
        return page

    def open_now_panel(self, page):
        page.evaluate("""() => {
          document.getElementById('app-shell').hidden = false;
          document.getElementById('lock-view').hidden = true;
          document.getElementById('telegram-view').hidden = true;
          document.getElementById('now-panel').hidden = false;
        }""")
        page.wait_for_timeout(150)

    def test_now_playing_does_not_cover_the_transport_on_a_phone(self):
        page = self.page(390, 844)
        self.open_now_panel(page)
        hit = page.evaluate("""() => {
          const button = document.getElementById('play');
          const box = button.getBoundingClientRect();
          const target = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
          return target === button || button.contains(target) ? 'play' : target.tagName + '#' + target.id;
        }""")
        self.assertEqual("play", hit, "the Now Playing panel covers the play button, so playback is unreachable")

        # Geometry too, not just the hit test: a future z-index change must not silently re-break
        # this while elementFromPoint still happens to pass.
        panel_bottom, player_top = page.evaluate("""() => [
          document.getElementById('now-panel').getBoundingClientRect().bottom,
          document.getElementById('player').getBoundingClientRect().top,
        ]""")
        self.assertLessEqual(panel_bottom, player_top + 1, "the panel overlaps the player box")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./.venv/bin/python -m unittest tests.test_layout -v`
Expected: FAIL with `AssertionError: 'SECTION#now-panel' != 'play'` — the panel is on top of the button.

- [ ] **Step 3: Stop the panel above the player, in both 860px blocks**

`static/style.css` ~872-886 — the fullscreen panel rule inside the first `@media (max-width: 860px)` block gains a bottom bound:

```css
  .now-panel {
    position: fixed;
    z-index: 25;
    inset: 0;
    /* Stop above the player rather than covering it. The panel has no transport of its own, so
       a full-bleed inset: 0 made pause, skip, seek and progress unreachable on the app's
       most-used mobile screen -- and adding a second transport here would be two things to
       keep in sync instead of one. */
    bottom: var(--player-height);
  }
```

`static/style.css:1222` — the **second** copy, in the `(hover: none), (pointer: coarse)` block. Fixing only one changes nothing:

```css
  .now-panel { position: fixed; z-index: 25; inset: 0; bottom: var(--player-height); }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `./.venv/bin/python -m unittest tests.test_layout -v`
Expected: PASS, `OK` with 1 test

- [ ] **Step 5: Verify the whole suite still runs, including where Playwright is absent**

Run: `./.venv/bin/python -m unittest discover -s tests`
Expected: PASS, `OK` — the count rises by the new test. Confirm the `skipIf` works by checking the class is skipped rather than erroring when `playwright` cannot be imported.

- [ ] **Step 6: Commit**

```bash
git add tests/test_layout.py static/style.css
git commit -m "Playwright layout harness, and stop Now Playing covering the transport

At 390x844 the panel was position: fixed; inset: 0; z-index: 25 over a z-index: 10
player, and it carries no transport of its own -- so opening it made pause, skip,
seek and progress unreachable on the most-used mobile screen. elementFromPoint at
the play button returned SECTION#now-panel. bottom: var(--player-height) fixes it
structurally, in both 860px blocks (the rule is declared twice; fixing one copy
fixes nothing).

The harness is the point as much as the fix: geometry claims about this app were
wrong three times because a stub was wrong, so the stubs here are checked against
their real producers -- notably cache_status returns 'files', not the 'count' the
old audit rig stubs. skipIf keeps the suite runnable without a browser."
```

---

### Task 9: A9 — you cannot filter a playlist on a phone

**Files:**
- Modify: `static/style.css:871`
- Test: `tests/test_layout.py` (Task 8's class)

**Interfaces:**
- Consumes: `self.page(width, height)` from Task 8
- Produces: nothing

`static/style.css:871`, inside the `@media (max-width: 860px)` block, is:

```css
  .library-heading .small-copy, .search-control { display: none; }
```

`.search-control` (`index.html:178-182`) is the label wrapping `#track-search`, whose placeholder is "Filter this playlist". So on a phone the per-playlist filter is **simply gone**. Global search remains, but it searches all of Telegram rather than the open playlist, so the narrowing task has no mobile equivalent at all. The same rule also drops the track count (`.library-heading .small-copy`, which is `#library-summary`) while keeping the decorative `CHANNEL` eyebrow above it.

Spec §4.5 inverts that priority: the filter comes back as a header field, the count stays, and **the eyebrow is what drops at small widths**. Task 4 already removed the coarse-pointer count hide at `style.css:1243`; this task removes the width-based one.

- [ ] **Step 1: Write the failing test**

Add to `LayoutTests`:

```python
    def test_phones_can_filter_the_open_playlist(self):
        page = self.page(390, 844)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_timeout(150)
        visible = page.evaluate("""() => {
          const shown = (selector) => {
            const element = document.querySelector(selector);
            if (!element) return "missing";
            const style = getComputedStyle(element);
            return style.display !== "none" && style.visibility !== "hidden";
          };
          return { filter: shown(".search-control"), count: shown("#library-summary") };
        }""")
        self.assertIs(True, visible["filter"], "the playlist filter is hidden on a phone, so narrowing is impossible")
        self.assertIs(True, visible["count"], "the track count is hidden on a phone")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./.venv/bin/python -m unittest tests.test_layout -v`
Expected: FAIL with `AssertionError: False is not True : the playlist filter is hidden on a phone, so narrowing is impossible`

- [ ] **Step 3: Keep the filter and the count, drop the eyebrow instead**

`static/style.css:871` — replace the rule. The filter becomes a full-width row under the heading so it has room at 390px, and the classifying-but-decorative eyebrow is what yields:

```css
  /* Spec 4.5: the filter and the count are the working parts of this header; the eyebrow is
     decoration. Hiding .search-control removed the narrowing task from phones entirely --
     global search is not a substitute, it searches all of Telegram rather than this playlist. */
  .library-heading .eyebrow { display: none; }
  .header-actions { flex-wrap: wrap; }
  .search-control { flex: 1 1 100%; order: 10; }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `./.venv/bin/python -m unittest tests.test_layout -v`
Expected: PASS

- [ ] **Step 5: Verify**

Run: `./.venv/bin/python -m unittest discover -s tests`
Expected: PASS, `OK`

Check by eye at 390px and 320px that the filter is reachable and the heading has not reflowed into two awkward lines.

- [ ] **Step 6: Commit**

```bash
git add static/style.css tests/test_layout.py
git commit -m "Phones get the playlist filter back

style.css:871 hid .search-control below 860px, which removed the narrowing task
from phones entirely -- global search is not equivalent, it searches all of
Telegram rather than the open playlist. The same rule dropped the track count
while keeping a decorative eyebrow, so the priority is now inverted per spec 4.5."
```

---

### Task 10: A10 — zero-result states keep their furniture

**Files:**
- Modify: `static/app.js:636-650` (the empty branch of `renderTracks`), `static/app.js:651-670` (re-enable on the populated path)
- Modify: `static/style.css` (one rule for the hidden column head)
- Test: `tests/test_layout.py`

**Interfaces:**
- Consumes: `self.page(width, height)` from Task 8
- Produces: nothing

With a filter that matches nothing, two things persist that should not: the `TRACK / SOURCE / TIME` column head stays above an empty list, and Play / Shuffle remain enabled and primary-black with nothing to play.

Verified structure: the head is separate markup at `index.html:194-196` — `<div class="track-head utility" aria-hidden="true"><span>Track</span><span>Source</span><span>Time</span><span></span></div>` — so `renderTracks`' early return never touches it. `.track-head` shares its grid with `.track-row` (`style.css:469-475`) and already has a `display: none` at `style.css:911` for small widths, so **drive this from a class on the library, not by editing line 911**, or the two rules will fight.

- [ ] **Step 1: Write the failing test**

```python
    def test_zero_results_drop_the_column_head_and_disable_play(self):
        page = self.page(1440, 900)
        page.route("**/api/tracks*", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"items": [], "offset": 0, "total": 0}'))
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.fill("#track-search", "qqqqq")
        page.wait_for_selector("#empty-library:not([hidden])")
        state = page.evaluate("""() => ({
          head: getComputedStyle(document.querySelector('.track-head')).display,
          play: document.getElementById('play-playlist').disabled,
          shuffle: document.getElementById('shuffle-playlist').disabled,
        })""")
        self.assertEqual("none", state["head"], "the TRACK/SOURCE/TIME head sits above an empty list")
        self.assertIs(True, state["play"], "Play is enabled with nothing to play")
        self.assertIs(True, state["shuffle"], "Shuffle is enabled with nothing to shuffle")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./.venv/bin/python -m unittest tests.test_layout -v`
Expected: FAIL with `AssertionError: 'grid' != 'none' : the TRACK/SOURCE/TIME head sits above an empty list`

- [ ] **Step 3: Mark the empty state and disable the transport buttons**

`static/app.js:636-650` — in the empty branch, after `$("empty-library").hidden = !empty; list.hidden = empty;`:

```js
  $("library").classList.toggle("is-empty", empty);
  $("play-playlist").disabled = empty;
  $("shuffle-playlist").disabled = empty;
```

Both must also be reset on the populated path, or the disabled state sticks after the filter is cleared. Add immediately after the empty branch's `return`, where the real rows are about to render:

```js
  $("library").classList.remove("is-empty");
  $("play-playlist").disabled = false;
  $("shuffle-playlist").disabled = false;
```

`static/style.css` — one rule beside the other `.track-head` declarations. A class on the scroller rather than an edit to `style.css:911`, so the responsive hide and the empty hide cannot contradict each other:

```css
/* A column head over zero rows labels nothing. Driven by a class rather than by editing the
   860px .track-head { display: none } so the two conditions do not fight. */
.library.is-empty .track-head { display: none; }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `./.venv/bin/python -m unittest tests.test_layout -v`
Expected: PASS

- [ ] **Step 5: Verify**

Run: `./.venv/bin/python -m unittest discover -s tests`
Expected: PASS, `OK`

Then confirm by hand that clearing the filter re-enables both buttons and brings the head back — the sticky-disabled case is the one this can get wrong.

- [ ] **Step 6: Commit**

```bash
git add static/app.js static/style.css tests/test_layout.py
git commit -m "Zero results: drop the column head, disable Play and Shuffle

A TRACK/SOURCE/TIME head labels nothing when it sits over an empty list, and
Play on '0 tracks' has nothing to do -- both stayed because the head is separate
markup that renderTracks' early return never touched. The head is hidden via a
class on .library rather than by editing the 860px rule, so the responsive and
empty conditions cannot fight, and both buttons are explicitly re-enabled on the
populated path so the disabled state cannot stick after the filter is cleared."
```

---

### Task 11: B8 — stop leaking raw exception text into the UI

**Files:**
- Modify: `static/format.js` (add `errorCopy`), `static/format.test.js`
- Modify: `static/app.js:29-33` (move `AppError`), `static/app.js:81-87` (`showError`), `static/app.js:877` (playback retry)
- Modify: `static/index.html:467` (reconcile the dialog title)
- Test: `static/format.test.js`

**Interfaces:**
- Consumes: `static/format.js` from Task 4
- Produces: `errorCopy(error) -> string` and `class AppError extends Error` — **both exported from `static/format.js`**, which `static/app.js` imports. Task 7's `metadataFailed` calls `errorCopy`.

`showError` (`app.js:81-87`) does `$("error-message").textContent = error?.message || String(error)`, and there are **54 `showError` call sites**, so the fix belongs inside `showError`, not at the call sites. Consequences today:

- Playback failure (`app.js:877`, `catch (error) { showError(error) }`) surfaces **"The element has no supported sources."** — the raw `HTMLMediaElement` string — and passes no `retry`, so `#error-retry` stays hidden for something inherently retryable.
- Any client-side `TypeError` reaches the dialog verbatim.

`class AppError extends Error` (`app.js:29-33`, `constructor(message, retryable = false, code = "request_failed")`) is thrown **only** in `api()` (`app.js:35-51`) from `failure?.message || body?.detail || \`Request failed (${response.status})\``, where `failure = body?.error`. So `instanceof AppError` is exactly the test for "server-authored and meant for a person".

**Where `AppError` should live.** `errorCopy` must be testable under `node --test`, and `app.js` touches `document` at import time, so a test cannot import it. Moving `AppError` into `format.js` and having `app.js` import it is cleaner than duck-typing on a `code` property: the class is a plain `Error` subclass with no DOM dependency, one import line changes, and the type test stays a real `instanceof` rather than a guess about shape.

Also reconcile the two titles: `index.html:467` hardcodes `<h2 id="error-title">Something went wrong</h2>` while `showError`'s default parameter is `"Couldn’t complete that"`. Two strings for one dialog; keep the parameter's wording and make the markup match, so the first paint and every subsequent one agree.

- [ ] **Step 1: Write the failing test**

Append to `static/format.test.js`, extending its import:

```js
import { AppError, errorCopy, sourceKindLabel } from "./format.js";
```

```js
test("only server-authored errors put their own message on screen", () => {
  // AppError is thrown solely by api() from a server-supplied body.error.message, so it is the
  // one kind of failure whose text was written for a person to read.
  assert.equal(errorCopy(new AppError("That channel is private.")), "That channel is private.");
  assert.equal(errorCopy(new AppError("Rate limited", true, "rate_limited")), "Rate limited");

  // Everything else is an internal string. "The element has no supported sources." is the real
  // HTMLMediaElement message the audit found in the dialog.
  const fallback = "Something went wrong at our end. Try again in a moment.";
  assert.equal(errorCopy(new Error("The element has no supported sources.")), fallback);
  assert.equal(errorCopy(new TypeError("candidates.map is not a function")), fallback);
  assert.equal(errorCopy("a bare string throw"), fallback);
  assert.equal(errorCopy(null), fallback);
  assert.equal(errorCopy(undefined), fallback);

  // An AppError with no usable message must not render an empty dialog.
  assert.equal(errorCopy(new AppError("")), fallback);
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test static/*.test.js`
Expected: FAIL with `SyntaxError: The requested module './format.js' does not provide an export named 'AppError'`

- [ ] **Step 3: Move `AppError` into `format.js` and add `errorCopy`**

Append to `static/format.js`:

```js
// Thrown only by api(), from a server-supplied body.error.message. Lives here rather than in
// app.js so errorCopy can be tested under node without importing a module that touches document.
export class AppError extends Error {
  constructor(message, retryable = false, code = "request_failed") {
    super(message); this.retryable = retryable; this.code = code;
  }
}

const GENERIC_FAILURE = "Something went wrong at our end. Try again in a moment.";

// error.message is only shown when the server authored it for a person. Anything else is an
// internal string: the audit found "The element has no supported sources." -- the raw
// HTMLMediaElement message -- and client-side TypeErrors in the error dialog.
export function errorCopy(error) {
  return error instanceof AppError && error.message ? error.message : GENERIC_FAILURE;
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `node --test static/*.test.js`
Expected: PASS

- [ ] **Step 5: Use it in `showError`, and give playback a retry**

`static/app.js:29-33` — delete the local `class AppError` and import it instead, extending Task 4's import line:

```js
import { AppError, errorCopy, sourceKindLabel } from "./format.js";
```

`static/app.js:81-87` — one line changes:

```js
function showError(error, retry = null, title = "Couldn’t complete that") {
  retryAction = retry;
  $("error-title").textContent = title;
  $("error-message").textContent = errorCopy(error);
  $("error-retry").hidden = !(retry && error?.retryable !== false);
  if (!$("error-dialog").open) $("error-dialog").showModal();
}
```

`static/app.js:877` — the playback path names its operation and offers the retry the audit found missing. A failed stream is the definition of retryable:

```js
  try { await startAudioPlayback(); } catch (error) { showError(error, () => startAudioPlayback().catch(() => {}), "Couldn’t play this track"); }
```

`static/index.html:467` — match the JS default so the two do not disagree:

```html
<h2 id="error-title">Couldn’t complete that</h2>
```

- [ ] **Step 6: Verify**

Run: `node --test static/*.test.js`
Expected: PASS

Run: `./.venv/bin/python -m unittest discover -s tests`
Expected: PASS, `OK`

Then check by hand: play a track whose audio route 404s, and confirm the dialog reads the written fallback with a working "Try again", not `The element has no supported sources.`

- [ ] **Step 7: Commit**

```bash
git add static/format.js static/format.test.js static/app.js static/index.html
git commit -m "Show written errors, not exception text

showError piped error.message straight into the dialog across 54 call sites, so
users saw 'The element has no supported sources.' -- the raw HTMLMediaElement
string -- and any client-side TypeError. Only AppError, thrown solely by api()
from a server-supplied body.error.message, was ever written for a person, so
that is now the only error that speaks for itself.

AppError moves to format.js so errorCopy is testable under node without
importing a module that touches document. Playback failures also get the Retry
the audit found missing, and the dialog's hardcoded 'Something went wrong' is
reconciled with the 'Couldn't complete that' the JS was already passing."
```

---

## Phase 2 — tokens (tasks 12–15)

### Task 12: `static/design-tokens.test.js` — the phase guard, written first

**Files:**
- Create: `static/design-tokens.test.js`
- Test: itself

**Interfaces:**
- Consumes: nothing
- Produces: the contract Tasks 13–15 are written to satisfy. It must **fail** against the current CSS and pass only once they land.

This test is the reason Phase 2 is trustworthy. Spec §6 asks for four properties enforced by a test rather than asserted in prose: no token pair below its stated ratio, no rendered font-size below 11px, `--stamp` only in the permitted rules, and no hex literal outside the token blocks. It is written **before** the palette changes so it describes the target, not the result.

**Findings from validating the parser against the real file** — the audit undercounted twice, so use these numbers:

- **8 stray hex literals, not six.** The audit's C5 lists `#f7f7f4`, `rgb(17 17 17 / .82)`, `#3b8753`, `#111214`, `#1a1b1d`, `#14161a`. It misses **three `#fff`**: `style.css:1032` (`.track-play-overlay` colour), `style.css:1034` (`.track-row.buffering .track-play-overlay::after` border-right-color), and `style.css:1273` (`.qr-code svg` background). True locations, comments excluded: 236, 1032, 1034, 1097, 1257, 1264, 1273, 1301.
- **21 sub-11px font-size occurrences across 3 distinct sizes** (8px ×5, 9px ×5, 10px ×11). The audit's "nine type sizes below 11px" counts selectors, not declarations. The test must count every declaration or it will pass while ten of them survive.

**Two parser requirements, both learned the hard way:**

1. **Blank comments in place, do not delete them.** Stripping `/* … */` with `.replace(…, "")` shifts every subsequent line number, so failure messages point at the wrong rule. Replacing each comment's non-newline characters with spaces preserves line numbers exactly (verified: line count identical).
2. **Assert computed ratios against requirements, never the spec's printed figures** — two of the spec's seven are arithmetic slips (see Context). The test computes contrast from the hex values it parses and compares against the WCAG threshold.

No CSS parser dependency. `ponytail:` regex over the token blocks is the ceiling and it is sufficient — this file has a fixed, known shape.

**The `required` table below names two tokens that do not exist yet:** `--ok` (added in Task 13, replacing the untokenised `#3b8753`) and `--rail` (added in Task 15, giving the rail its own plane). That is intentional — the table is the target, and both are text backgrounds so both must be enforced rather than eyeballed. It does mean the contrast assertion stays red until Task 15 lands, so treat that one as the phase-level gate rather than a per-task one.

- [ ] **Step 1: Write the test**

Create `static/design-tokens.test.js`:

```js
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const RAW = readFileSync(new URL("./style.css", import.meta.url), "utf8");
// Blank comments in place: deleting them shifts line numbers, so failures would cite the wrong
// rule. Same length, same newlines, no comment content.
const CSS = RAW.replace(/\/\*[\s\S]*?\*\//g, (match) => match.replace(/[^\n]/g, " "));
const LINES = CSS.split("\n");

function tokenBlock(startPattern) {
  const start = LINES.findIndex((line) => startPattern.test(line));
  assert.notEqual(start, -1, `token block ${startPattern} not found`);
  const tokens = {};
  for (let index = start; index < LINES.length; index += 1) {
    const match = LINES[index].match(/(--[\w-]+):\s*([^;]+);/);
    if (match) tokens[match[1]] = match[2].trim();
    if (index > start && /^\}/.test(LINES[index])) break;
  }
  return tokens;
}

const luminance = (hex) => {
  const value = hex.replace("#", "");
  const full = value.length === 3 ? [...value].map((c) => c + c).join("") : value;
  const [r, g, b] = [0, 2, 4].map((offset) => parseInt(full.slice(offset, offset + 2), 16) / 255)
    .map((channel) => (channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};

const contrast = (a, b) => {
  const [high, low] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (high + 0.05) / (low + 0.05);
};

test("token contrast meets its requirement, computed not transcribed", () => {
  const light = tokenBlock(/^:root\s*\{/);
  const dark = tokenBlock(/^html\[data-theme="dark"\]\s*\{/);

  // Requirements from spec 3.1. The spec's own printed ratios contain two arithmetic slips, so
  // this asserts the threshold, never the quoted figure.
  const required = [
    [light, "--graphite", "--paper", 4.5, "secondary text"],
    [light, "--stamp", "--paper", 4.5, "the playing marker"],
    [light, "--ink", "--rule", 3.0, "progress elapsed vs remaining"],
    [light, "--graphite", "--rule", 3.0, "buffered vs remaining"],
    [light, "--ink", "--paper", 4.5, "body text"],
    [light, "--danger", "--paper", 4.5, "destructive text"],
    [light, "--ok", "--surface", 4.5, "the cache-ready state"],
    // The rail is a text background too, so it carries the same burden as --paper.
    [light, "--graphite", "--rail", 4.5, "rail secondary text"],
    [light, "--ink", "--rail", 4.5, "rail source titles"],
    [dark, "--graphite", "--paper", 4.5, "dark secondary text"],
    [dark, "--stamp", "--paper", 4.5, "dark playing marker"],
    [dark, "--ink", "--paper", 4.5, "dark body text"],
    [dark, "--danger", "--paper", 4.5, "dark destructive text"],
    [dark, "--ok", "--surface", 4.5, "dark cache-ready state"],
    [dark, "--graphite", "--rail", 4.5, "dark rail secondary text"],
    [dark, "--ink", "--rail", 4.5, "dark rail source titles"],
  ];
  for (const [block, front, back, minimum, what] of required) {
    const ratio = contrast(block[front], block[back]);
    assert.ok(ratio >= minimum,
      `${front} on ${back} (${what}) is ${ratio.toFixed(2)}:1, needs ${minimum}:1`);
  }

  // Elapsed must be separable from buffered, or the progress bar reads as one flat fill.
  const separation = contrast(light["--ink"], light["--graphite"]);
  assert.ok(separation >= 2.0, `elapsed vs buffered is only ${separation.toFixed(2)}:1`);

  // The focus ring is the app's primary keyboard affordance; it was 1.38:1 via color-mix.
  assert.match(light["--focus-ring"], /var\(--ink\)/,
    "the focus ring must land on --ink, not a translucent accent");
});

test("nothing ships below the 11px floor", () => {
  const offenders = [];
  LINES.forEach((line, index) => {
    const declaration = line.match(/font-size:\s*([^;}]+)/);
    if (!declaration) return;
    // Every px number in the value, so clamp() minima are covered too.
    for (const size of declaration[1].matchAll(/([\d.]+)px/g)) {
      if (Number(size[1]) < 11) offenders.push(`${index + 1}: ${size[1]}px`);
    }
  });
  assert.deepEqual(offenders, [], `font sizes below the 11px floor:\n${offenders.join("\n")}`);
});

test("--stamp marks only the currently playing track", () => {
  const allowed = [
    ".track-row.current", ".queue-row.current", ".progress::-webkit-slider-thumb",
    ".progress::-moz-range-thumb", ".label-disc.is-playing", ".playing-mark",
  ];
  const violations = [];
  // Rule bodies, so a selector list is checked as a whole.
  for (const rule of CSS.matchAll(/([^{}]+)\{([^}]*)\}/g)) {
    const [, selector, body] = rule;
    if (!body.includes("var(--stamp)")) continue;
    const trimmed = selector.trim();
    if (trimmed.startsWith(":root") || trimmed.startsWith("html[")) continue;
    if (!allowed.some((permitted) => trimmed.includes(permitted))) violations.push(trimmed);
  }
  assert.deepEqual(violations, [],
    `--stamp has exactly one job. Found it in:\n${violations.join("\n")}`);
});

test("no hex literal outside the token blocks", () => {
  // The three token blocks end at the html[data-font] remaps; everything after is component CSS.
  const firstComponentLine = LINES.findIndex((line) => /^html\[data-font="serif"\]/.test(line));
  assert.notEqual(firstComponentLine, -1, "could not locate the end of the token blocks");
  const strays = [];
  LINES.slice(firstComponentLine).forEach((line, offset) => {
    for (const hex of line.match(/#[0-9a-fA-F]{3,8}\b/g) || []) {
      strays.push(`${firstComponentLine + offset + 1}: ${hex}`);
    }
  });
  assert.deepEqual(strays, [], `hex literals belong in the token blocks:\n${strays.join("\n")}`);
});
```

- [ ] **Step 2: Run it and confirm it fails on all four counts**

Run: `node --test static/*.test.js`

Expected: FAIL. **This exact test file was executed against the current stylesheet during planning, so the expected output below is observed, not predicted:** `tests 4`, `pass 1`, `fail 3`. The four results, which are the Phase 2 worklist:

1. **contrast** — `--graphite`/`--stamp`/`--rule` do not exist yet, so `tokenBlock` returns `undefined` and `luminance` throws; the focus-ring assertion also fails because line 20 is `color-mix(in srgb, var(--accent) 70%, transparent)` (composites to `#c0d5f8`, **1.38:1**).
2. **11px floor** — exactly 21 offenders. **I ran this test against the current stylesheet to confirm**, and these are the true lines to fix: `132, 332, 641, 676, 762, 787, 976, 1012, 1092, 1276, 1291` (10px ×11); `312, 314, 1129, 1160, 1309` (9px ×5); `333, 1057, 1089, 1096, 1256` (8px ×5).
3. **`--stamp`** — the token does not exist, so this one passes vacuously *now* and becomes meaningful after Task 13. Note that in the commit message rather than pretending it fails.
4. **stray hex** — 8 literals at lines 236, 1032, 1034, 1097, 1257, 1264, 1273, 1301.

- [ ] **Step 3: Commit the guard before changing any value**

The test lands red on purpose. Tasks 13–15 are what turn it green, and committing it first means the diff that changes colours cannot quietly redefine what "correct" means.

```bash
git add static/design-tokens.test.js
git commit -m "Add the design-token contract as a test, currently red

Spec section 6 asks for four properties enforced by a test rather than asserted
in prose: contrast ratios, an 11px floor, --stamp used for one job only, and no
hex literals outside the token blocks. Committing it before the palette changes
means the colour diff cannot quietly redefine what correct means.

It computes contrast from the parsed hex rather than trusting the spec's printed
table, because two of those seven figures are arithmetic slips (both safe -- the
real ratios still pass). Comments are blanked in place rather than deleted so
line numbers in failure messages stay accurate.

Current failures are the Phase 2 worklist: 21 font-size declarations under 11px
(the audit's 'nine' counted selectors, not declarations) and 8 stray hex literals
(the audit's C5 missed three #fff, at 1032, 1034 and 1273). The --stamp assertion
passes vacuously until the token exists."
```

---

### Task 13: the palette swap

**Files:**
- Modify: `static/style.css:1-31` (`:root`), `:33-47` (`html[data-theme="dark"]`), `:50-66` (the `prefers-color-scheme` copy), plus the ~166 `var()` references catalogued below
- Test: `static/design-tokens.test.js` (Task 12)

**Interfaces:**
- Consumes: Task 12's contract
- Produces: the token vocabulary every later task uses — `--paper --surface --ink --graphite --rule --rule-soft --stamp --danger --scrim`, and the geometry tokens `--radius-control --radius-panel --row-height --art-row --rail-width --panel-width --player-height`

**The rename decision, which is the whole shape of this task.** Four old token names disappear and I counted their references: `--muted` **57**, `--line` **57**, `--accent` **42**, `--soft-line` **10**. Keeping them as aliases would be the smaller diff, but it is wrong for one of them, and here is why:

- `--muted` → `--graphite`, `--line` → `--rule`, `--soft-line` → `--rule-soft` are **pure renames**. Alias them in the same commit (`--muted: var(--graphite)`) only if you intend to delete the aliases in the same commit too; a permanent alias means two names for one colour, which is the confusion the spec exists to end. Prefer a mechanical find-and-replace.
- **`--accent` must NOT be aliased.** The audit's C1 is the point of the redesign: one accent doing eight jobs means it signals nothing. I catalogued every use, and they resolve to *different* new tokens:

| Old `--accent` use | style.css | Becomes |
|---|---|---|
| focus ring | 20 | `--ink` (spec §3.1: `0 0 0 2px var(--paper), 0 0 0 4px var(--ink)`) |
| `.button.primary` bg / hover | 177, 217 | `--ink` fill with `--paper` glyph — a pressed stamp |
| `.source-link.active` (+`:hover`, `.source-mark`) | 379, 380, 384 | surface shift, `--surface`; **not colour** |
| `.track-row.current` | 493 | `--stamp` (this is the one permitted job) |
| `.progress` track / progress / thumb | 716-720 | elapsed `--ink`, buffered `--graphite`, remaining `--rule`, thumb `--stamp` |
| `.tab::after` underline | 607 | `--ink` |
| `.icon-button.active` | 729 | `--ink` (the liked glyph already carries fill; audit B3 measured this at 1.72:1) |
| `.segmented button.active` | 807 | `--ink` fill, `--paper` glyph |
| `.toast-timer` | 832 | `--ink` |
| skeleton shimmer, `.art-placeholder::after` | 596, 936, 939 | `--rule-soft` |
| `.source-entry.drag-over`, `.source-select` | 951, 978 | `--ink` |
| `.play-button:hover`, `[aria-busy]::after` | 742, 747 | `--ink` |
| `.global-search-signal span`, `.button.saved` | 311, 940 | `--graphite` / `--ink` |

Work down that table rather than replacing `--accent` globally. A global replace is how "one colour meaning everything" survives a redesign that was supposed to kill it.

- [ ] **Step 1: Confirm the guard is red for the right reasons**

Run: `node --test static/*.test.js`
Expected: FAIL — contrast (tokens absent), 11px floor (21 offenders), stray hex (8 literals). Task 14 owns the floor; this task owns contrast and hex.

- [ ] **Step 2: Replace the light token block**

`static/style.css:1-31` — spec §3.1 and §3.3 values, verbatim:

```css
:root {
  color-scheme: light;
  --paper: #faf9f6;        /* blank sleeve stock */
  --surface: #ffffff;      /* raised: rows on hover, panels, modals */
  --surface-raised: #ffffff;
  --ink: #14110f;          /* stamp ink, near-black with a little brown */
  --graphite: #6b655d;     /* secondary text */
  --rule: #ddd8ce;         /* printed rule */
  --rule-soft: #ebe7df;    /* row divider */
  --stamp: #8c2f24;        /* oxblood. ONE job: currently playing. */
  --danger: #a2331f;       /* destructive only */
  --scrim: rgb(20 17 15 / .82);  /* over artwork; a token, not a literal */
  --radius-control: 6px;   /* was 8 -- a stamped edge is tighter */
  --radius-panel: 12px;
  --radius-pill: 999px;
  --elev-0: none;
  --elev-1: 0 1px 0 color-mix(in srgb, var(--ink) 6%, transparent);
  --elev-2: 0 8px 28px rgb(0 0 0 / .12);
  --elev-3: 0 24px 80px rgb(0 0 0 / .2);
  --focus-ring: 0 0 0 2px var(--paper), 0 0 0 4px var(--ink);
  --ease-out: cubic-bezier(.2, .8, .2, 1);
  --ease-spring: cubic-bezier(.34, 1.2, .64, 1);
  --dur-1: 120ms;
  --dur-2: 180ms;
  --dur-3: 280ms;
  --font-ui: Inter, Avenir, "Segoe UI", ui-sans-serif, system-ui, sans-serif;
  --font-serif: Iowan Old Style, Palatino Linotype, Georgia, serif;
  --font-mono: "SFMono-Regular", Consolas, "Liberation Mono", ui-monospace, monospace;
  --row-height: 52px;      /* was 68 */
  --art-row: 40px;
  --rail-width: 236px;
  --panel-width: 360px;
  --player-height: 84px;
  --compact-header: 132px; /* measured in Task 2; do not guess this */
}
```

`--font-ui` keeps its old value here; Task 14 replaces the stack once the faces are on disk, so the two changes stay separable.

- [ ] **Step 3: Replace BOTH dark blocks**

Spec §3.1's dark palette is "black acetate, not inverted paper". `static/style.css:33-47` and the `prefers-color-scheme` copy at `:50-66` take the **same** values — change one and the system theme silently diverges from the explicit one:

```css
  --paper: #12100e;
  --surface: #1b1815;
  --surface-raised: color-mix(in srgb, var(--surface) 92%, var(--ink) 8%);
  --ink: #f2efe8;
  --graphite: #a39c92;
  --rule: #332e29;
  --rule-soft: #24211d;
  --stamp: #d4574a;
  --danger: #e0796a;
  --elev-1: 0 1px 0 color-mix(in srgb, var(--ink) 10%, transparent);
  --elev-2: 0 8px 28px rgb(0 0 0 / .24);
  --elev-3: 0 24px 80px rgb(0 0 0 / .4);
```

- [ ] **Step 4: Rebuild the progress bar so it says something at rest**

Audit B2, measured: elapsed vs remaining **1.18:1**, elapsed vs buffered **1.11:1**, and the thumb is `opacity: 0` until hover. So the most-watched element in a music player shows no position and no played/buffered distinction. `static/style.css:716-720`, treating **both** vendor prefixes:

```css
.progress::-webkit-slider-runnable-track { height: 3px; background: linear-gradient(to right, var(--ink) 0 var(--progress, 0%), var(--graphite) var(--progress, 0%) var(--buffered, 0%), var(--rule) var(--buffered, 0%) 100%); }
.progress::-moz-range-track { height: 3px; background: var(--rule); }
.progress::-moz-range-progress { height: 3px; background: var(--ink); }
/* Always visible: an invisible thumb on a 1.18:1 track meant the bar communicated nothing at
   rest, which is most of the time you are looking at it. */
.progress::-webkit-slider-thumb { width: 10px; height: 10px; margin-top: -3.5px; appearance: none; border: 1px solid var(--paper); border-radius: 50%; background: var(--stamp); opacity: 1; }
.progress::-moz-range-thumb { width: 10px; height: 10px; border: 1px solid var(--paper); border-radius: 50%; background: var(--stamp); opacity: 1; }
```

- [ ] **Step 5: Kill all eight stray hex literals**

Task 12 reports the true lines. The audit's C5 named six; **three `#fff` were missed**:

| Line | Literal | Replacement |
|---|---|---|
| 236 | `#f7f7f4` (`.qr-code`) | `var(--paper)` — it was duplicating it |
| 1032 | `#fff` (`.track-play-overlay` colour) | `var(--paper)`; its `rgb(17 17 17 / .82)` background becomes `var(--scrim)` |
| 1034 | `#fff` (buffering spinner border) | `var(--paper)` |
| 1097 | `#3b8753` (`.cache-state.ready`) | a real token; it is the app's only green, has no dark variant, and computes **4.17:1** on dark `--surface` at 8px |
| 1257 | `#111214` (`.qr-stage`) | `var(--ink)` |
| 1264 | `#1a1b1d` (`.qr-inset`) | `var(--surface)` |
| 1273 | `#fff` (`.qr-code svg` bg) | `var(--paper)` — a QR needs light quiet-zone contrast, so verify it still scans |
| 1301 | `#14161a` (`.combo-option.is-active`) | `var(--ink)`; `--accent-ink` was clearly intended |

For 1097, add an `--ok` token to all three blocks rather than leaving the app's one success colour untokenised. **I computed these, they are not guesses:**

```css
  --ok: #2f6b45;   /* light: 6.03:1 on --paper, 6.34:1 on --surface */
```
```css
  --ok: #6fbf8a;   /* dark: 8.58:1 on --paper, 7.99:1 on --surface */
```

Both clear the 4.5:1 text requirement in both themes, which matters because this string renders small. For contrast, the old `#3b8753` measures **4.02:1** on the *new* dark `--surface` — worse than the 4.17:1 the audit measured against the old one, so leaving it would have quietly regressed. Add `--ok` to Task 12's `required` table so it is enforced from now on.

- [ ] **Step 6: Remove the three "no left bar" violations**

`style.css:491` states the rule — "Background only… no left bar anywhere" — and three rules break it in **two different properties**, so grepping one form misses the third. Delete the `box-shadow` at `style.css:1002` (`.sidebar-collapsed .source-link.active`) and `style.css:1087` (`.queue-row.current`), replacing both with the background treatment the comment describes; change `border-left: 3px solid var(--accent)` at `style.css:1390` (`.restart-notice`) to a full `1px solid var(--rule)` border.

- [ ] **Step 7: Run the guard**

Run: `node --test static/*.test.js`
Expected: stray-hex PASSES, `--stamp` PASSES and is now meaningful. Two stay red on purpose: the 11px floor (21 offenders — Task 14's job) and contrast, which still references `--rail` from Task 15. Both are correct to leave failing here; the contrast assertion is the phase gate, not this task's gate. Confirm the *remaining* contrast failure names only `--rail`, not any token this task was supposed to add.

Run: `./.venv/bin/python -m unittest discover -s tests`
Expected: PASS, `OK`

- [ ] **Step 8: Look at it**

Render at 1440 and 390 in both themes with a track playing. Check specifically: exactly one `--stamp` element on screen (the current row), selection and hover reading as surface shifts rather than colour, the focus ring visible on the current row (audit B1 found the old ring vanished into the state it was marking), and the progress bar showing position and buffered extent without hovering.

- [ ] **Step 9: Commit**

```bash
git add static/style.css
git commit -m "Swap in the white-label palette

One accent was doing eight jobs -- focus ring, active source, current track, tab
underline, primary button, mode toggles, toast timer, skeleton shimmer -- so it
signalled nothing, and this particular periwinkle read as a framework default.
Each use is now a different token, and --stamp has exactly one job: the track
that is playing. Selection and hover are surface shifts, not colour.

Fixes measured accessibility failures: the focus ring was 1.38:1 against a 3:1
requirement (color-mix on a translucent accent), and the progress bar was 1.18:1
elapsed-vs-remaining with an opacity: 0 thumb, so at rest it showed neither
position nor buffered extent. Elapsed is now --ink, buffered --graphite,
remaining --rule, thumb always visible in --stamp, both vendor prefixes.

Also removes eight hardcoded colours -- three more #fff than the audit found --
and the three rules that broke the 'no left bar anywhere' comment at style.css:491,
which used two different properties, so a grep for one missed the third.

The 11px floor stays red until Task 14; the token contract's other three
assertions pass."
```

---

### Task 14: self-hosted fonts and the type scale

**Files:**
- Create: `static/fonts/archivo-variable.woff2`, `static/fonts/plex-mono-400.woff2`, `static/fonts/plex-mono-500.woff2`
- Modify: `static/style.css` (`@font-face` block, `--font-ui`/`--font-mono`, the type scale, and the 21 sub-11px declarations), `static/style.css:1170`, `static/index.html` (preload)
- Test: `static/design-tokens.test.js` (the 11px floor assertion turns green here)

**Interfaces:**
- Consumes: Task 13's token block
- Produces: `--text-display --text-title --text-body --text-small --text-micro --text-data`

Audit C3: `--font-ui: Inter, …` has **no `@font-face` and no preload anywhere**, so the face carrying the whole identity silently becomes Segoe UI or a generic sans on most machines. Spec §3.2: three self-hosted variable faces, ~180 KB, `font-display: swap`, preloaded.

**I verified the download end to end. Two corrections to the handoff's recipe:**

1. **The Google Fonts CSS2 API needs a browser User-Agent.** With Python's default UA it returns **54 unsplit faces with no `unicode-range` at all** (TTF, not woff2), so the handoff's "pick the face whose `unicode-range` contains `U+0000-00FF`" matches nothing and the script silently finds no candidate. With a Chrome UA it returns exactly **3 woff2 faces**, one of which carries `U+0000-00FF`. Send the UA.
2. **The single-file claim checks out.** The Latin Archivo face reports `font-stretch: 62% 125%` and `font-weight: 100 900`, so one file genuinely covers both condensed display and body via `font-stretch` — no second download.

Measured sizes: Archivo variable **90,104 bytes**, Plex Mono 400 **14,708**, Plex Mono 500 **14,888** — **119,700 total**, comfortably inside the ~180 KB budget. All three begin with `wOF2`.

- [ ] **Step 1: Download the three faces**

Run this exact script; I executed it during planning and the assertions below all held:

```bash
./.venv/bin/python - <<'PY'
import re
import urllib.request
from pathlib import Path

# The CSS2 API serves unsplit TTF to unknown clients: without a browser UA you get 54 faces
# with no unicode-range and the Latin-subset selection below matches nothing.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")
FONTS = Path("static/fonts")
FONTS.mkdir(parents=True, exist_ok=True)


def fetch(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30).read()


def latin_faces(css):
    for face in re.findall(r"@font-face\s*\{(.*?)\}", css, re.S):
        ranges = re.search(r"unicode-range:\s*([^;]+)", face)
        # The Latin subset is the only one that carries ASCII. Picking another ships a file
        # with no a-z in it, which renders as blank boxes.
        if ranges and "U+0000-00FF" in ranges.group(1):
            weight = re.search(r"font-weight:\s*([^;]+)", face)
            url = re.search(r"url\((https://[^)]+\.woff2)\)", face)
            yield weight.group(1).strip(), url.group(1)


targets = {
    "archivo-variable": ("https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,100..900&display=swap", None),
    "plex-mono-400": ("https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&display=swap", "400"),
    "plex-mono-500": ("https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&display=swap", "500"),
}

total = 0
for name, (source, want_weight) in targets.items():
    faces = list(latin_faces(fetch(source)))
    assert faces, f"{name}: no Latin-subset face found -- check the User-Agent"
    weight, url = next(((w, u) for w, u in faces if want_weight is None or w == want_weight))
    data = fetch(url)
    assert data[:4] == b"wOF2", f"{name}: not a woff2 file (got {data[:4]!r})"
    (FONTS / f"{name}.woff2").write_bytes(data)
    total += len(data)
    print(f"{name}.woff2  weight={weight}  {len(data):,} bytes")
print(f"total {total:,} bytes")
PY
```

Expected output (observed during planning):

```
archivo-variable.woff2  weight=100 900  90,104 bytes
plex-mono-400.woff2  weight=400  14,708 bytes
plex-mono-500.woff2  weight=500  14,888 bytes
total 119,700 bytes
```

- [ ] **Step 2: Declare the faces and keep the font remaps working**

`static/style.css`, immediately after the token blocks and **before** `html[data-font="serif"]` at line 68. Paths are **relative** — `app.py:299` mounts `/assets` → `static/`, so `url("fonts/…")` resolves correctly from a stylesheet served at `/assets/style.css`:

```css
/* Self-hosted: --font-ui named Inter with no @font-face anywhere, so the face carrying the
   identity silently became Segoe UI or a generic sans on most machines. One Archivo file
   covers display and body -- it ships a wdth axis, so font-stretch does the condensing. */
@font-face {
  font-family: "Archivo";
  font-style: normal;
  font-weight: 100 900;
  font-stretch: 62% 125%;
  font-display: swap;
  src: url("fonts/archivo-variable.woff2") format("woff2-variations");
}
@font-face {
  font-family: "IBM Plex Mono";
  font-style: normal;
  font-weight: 400;
  font-display: swap;
  src: url("fonts/plex-mono-400.woff2") format("woff2");
}
@font-face {
  font-family: "IBM Plex Mono";
  font-style: normal;
  font-weight: 500;
  font-display: swap;
  src: url("fonts/plex-mono-500.woff2") format("woff2");
}
```

In `:root`, point the stacks at them and add the scale (spec §3.2). `--font-ui` stays a **variable**, so `html[data-font="serif"|"mono"]` at lines 68-69 keep working untouched — that is the whole reason the remap is expressed as a token:

```css
  --font-ui: "Archivo", Avenir, "Segoe UI", ui-sans-serif, system-ui, sans-serif;
  --font-mono: "IBM Plex Mono", "SFMono-Regular", Consolas, ui-monospace, monospace;
  --font-display: "Archivo", var(--font-ui);
  --text-display: clamp(32px, 5vw, 44px);
  --text-title: 22px;
  --text-body: 15px;
  --text-small: 13px;
  --text-micro: 11px;   /* HARD FLOOR. nothing smaller ships. */
  --text-data: 12px;
```

Condensed display is `font-stretch: 62%` on `--font-display`, not a separate family:

```css
.eyebrow, .now-title h2, .library-heading h1, .modal-header h2 { font-family: var(--font-display); }
.library-heading h1 { font-size: var(--text-display); font-stretch: 62%; letter-spacing: .01em; }
```

`static/index.html` — preload only the two faces that paint immediately; preloading all three would compete with the stylesheet:

```html
<link rel="preload" href="/assets/fonts/archivo-variable.woff2" as="font" type="font/woff2" crossorigin>
<link rel="preload" href="/assets/fonts/plex-mono-400.woff2" as="font" type="font/woff2" crossorigin>
```

- [ ] **Step 3: Raise all 21 sub-11px declarations to the floor**

Task 12 lists the exact lines. Map 8px and 9px → `var(--text-micro)`, and 10px → `var(--text-micro)` or `var(--text-data)` where the content is numeric. The audit's worst region is the queue, which stacks 8 / 10 / 12px; `.global-result-mark` at 8px is deleted outright by Task 20, but raise it here anyway so the floor holds independently of task order.

`static/style.css:1170` — settings help inherits `font-weight: 600` from the global `label` rule at `style.css:182`, so every explanation shouts as loudly as its own label (audit B6, probed). Make it explicit:

```css
.setting-block label span { margin-top: 4px; color: var(--graphite); font-size: var(--text-data); font-weight: 400; }
```

- [ ] **Step 4: Run the guard**

Run: `node --test static/*.test.js`
Expected: the 11px floor now PASSES, along with stray-hex and `--stamp`. Contrast remains red only because `--rail` arrives in Task 15 — three of four green here, four of four after Task 15.

Run: `./.venv/bin/python -m unittest discover -s tests`
Expected: PASS, `OK`

- [ ] **Step 5: Confirm the faces actually load**

A green floor test does not prove the font arrived. In the browser, with devtools open:

```js
document.fonts.check("16px Archivo") && document.fonts.check("12px 'IBM Plex Mono'");
```

Expected `true`, with three woff2 requests returning **200** from `/assets/fonts/`. Then set the appearance control to Serif and Mono and confirm the `data-font` remaps still change the UI face — that is the regression this task most easily causes.

- [ ] **Step 6: Commit**

```bash
git add static/fonts static/style.css static/index.html
git commit -m "Self-host Archivo and IBM Plex Mono, and set a six-step scale

--font-ui named Inter with no @font-face and no preload anywhere, so on any
machine without Inter installed the face carrying the whole identity silently
became Segoe UI. All three faces are now local: 119,700 bytes total, inside the
~180 KB budget. One Archivo file covers display and body -- it carries a wdth
axis (font-stretch: 62% 125%), so condensed is an axis setting, not a download.

The type scale goes from eleven sizes to six with a hard 11px floor, which
deletes 21 declarations at 8, 9 and 10px -- the queue alone stacked 8/10/12.
Settings help gets an explicit font-weight: 400; it was inheriting 600 from the
global label rule, so every explanation read as loud as its own label.

--font-ui stays a token, so the data-font serif/mono remaps keep working.
Downloads need a browser User-Agent: the CSS2 API serves 54 unsplit faces with no
unicode-range to unknown clients, and the Latin-subset selection then matches
nothing."
```

---

### Task 15: dark-theme planes and styled scrollbars

**Files:**
- Modify: `static/style.css:265-273` (`.source-rail`), both dark blocks (`:33-47` and `:50-66`), plus one scrollbar block
- Test: `static/design-tokens.test.js` stays green; verified by rendering

**Interfaces:**
- Consumes: Task 13's tokens
- Produces: `--rail` (the rail's own plane) and `--scrollbar` styling used by all four scrollers

Two audit findings, both verified in the source:

**C6 — dark mode flattens the spatial model.** `.source-rail` sets `background: var(--paper)` (`style.css:273`), the *same* value as the page, separated only by a 1px `border-right`. In light mode that still reads, because rows hover to `#fff` against a `#f7f7f4` page. In dark mode both were `#0e0f10`, so the rail stopped being a plane and became a line. The second half is the `color-mix(accent 30%, transparent)` current-row tint losing almost all chroma over near-black, so the playing row read as **greyed out** — the opposite emphasis to light mode. Task 13 already replaced that tint with `--stamp`; this task fixes the planes.

**B7 — up to four unstyled scrollbars on screen at once** (rail `nav`, library, `now-content`, modal). Confirmed: the only scrollbar CSS in the file is `scrollbar-gutter: stable` at `style.css:625` and `:1153` — there is **no** colour styling at all. They overlap content: the settings scrollbar sits on the segmented control's right edge, the discover one beside the checkbox column, and the `now-content` one on the lyric text.

- [ ] **Step 1: Give the rail its own plane**

Add `--rail` to all three token blocks. In light it is the slightly-recessed stock the page sits on; in dark it is a genuinely distinct surface rather than the same black:

```css
  /* :root -- 1.082 separation from --paper, graphite still 5.06:1 on it */
  --rail: #f2f0ea;
```
```css
  /* both dark blocks -- 1.060 separation from --paper */
  --rail: #080706;
```

`static/style.css:273` — the rail stops borrowing the page colour:

```css
  border-right: 1px solid var(--rule);
  background: var(--rail);
```

In dark, `--paper` is `#12100e` and `--rail` is `#080706`: the rail is *darker* than the page, so the page reads as the raised plane holding content, and `--surface` (`#1b1815`) rows lift above both. Three legible depths instead of one.

**I computed these rather than eyeballing them, and the first value I tried was wrong** — worth knowing, because this is the exact failure being fixed. `#0d0b0a` gives only **1.034** separation from `--paper`, which is below the light theme's 1.082 and would have reproduced the flat-plane bug while looking deliberate in the diff. `#080706` gives **1.060**, close enough to light mode that both themes read the same way. Text on the rail is comfortable in both: light `--ink` 16.5:1 and `--graphite` 5.06:1; dark `--ink` 17.53:1, `--graphite` 7.41:1, `--stamp` 4.92:1.

- [ ] **Step 2: Style all four scrollbars once**

One block covers every scroller — a shared rule rather than four (the elements already share `overflow-y: auto`):

```css
/* Four of these can be on screen at once (rail, library, now-content, modal) and none were
   styled, so they painted native chrome over the segmented control, the discover checkboxes
   and the lyric text. scrollbar-color handles Firefox; the ::-webkit- rules handle the rest. */
* { scrollbar-width: thin; scrollbar-color: var(--rule) transparent; }
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { border: 3px solid transparent; border-radius: var(--radius-pill); background: var(--rule); background-clip: content-box; }
::-webkit-scrollbar-thumb:hover { background: var(--graphite); background-clip: content-box; }
::-webkit-scrollbar-corner { background: transparent; }
```

The transparent border plus `background-clip: content-box` is what insets the thumb, so it stops sitting flush against adjacent content. Keep the existing `scrollbar-gutter: stable` at `style.css:625` and `:1153` — the comment at `style.css:620-624` explains it reserves width only where a classic scrollbar actually takes space, which is correct and should survive.

- [ ] **Step 3: Verify the guard still passes**

Run: `node --test static/*.test.js`
Expected: **PASS — all four assertions, for the first time.** This is where the Phase 2 contract closes: `--rail` was the last token the contrast table referenced. `--rail` is a token rather than a literal, so the no-stray-hex rule keeps holding too.

Run: `./.venv/bin/python -m unittest discover -s tests`
Expected: PASS, `OK`

- [ ] **Step 4: Look at all four themes-by-viewport combinations**

Render 1440 and 390, light and dark, with a track playing and the panel open. Check: the rail reads as its own plane in **both** themes (the failure mode is dark-only); the playing row reads as emphasised rather than greyed out; and no scrollbar overlaps the segmented control, the discover checkboxes, or the lyric text. Compare against `docs/design/baseline/03-library-dark.png` and `12-now-playing-lyrics.png`.

- [ ] **Step 5: Commit**

```bash
git add static/style.css
git commit -m "Dark theme gets real planes, and all four scrollbars get styled

The rail used background: var(--paper) -- the same value as the page -- so in dark
mode, where rows do not hover to white, it stopped reading as a plane and became a
1px border. A --rail token makes it a distinct surface in both themes: in dark the
rail is darker than the page, so the page reads as the raised plane and rows lift
above both. Three legible depths instead of one.

Four scrollbars can be on screen at once and the file had no scrollbar colour
styling at all, only scrollbar-gutter -- so native chrome painted over the
segmented control, the discover checkboxes and the lyric text. One shared rule
covers all of them, inset via a transparent border and background-clip so the
thumb stops sitting flush against content."
```

---

## Phase 3 — structure (tasks 16–22)

### Task 16: finish `static/format.js` — dates and ordinals

**Files:**
- Modify: `static/format.js`, `static/format.test.js`
- Modify: `static/app.js:562` (`#library-summary`, replacing the raw `toLocaleString()`)
- Test: `static/format.test.js`

**Interfaces:**
- Consumes: `static/format.js` from Tasks 4 and 11
- Produces: `formatPostedDate(seconds, nowSeconds) -> string`, `ordinal(position, total) -> string`, `formatSyncedAt(seconds, nowSeconds) -> string`. Task 18 uses the first two for the POSTED and `#` columns; Task 21 uses `formatPostedDate` for the Details tab.

Audit E4: `app.js:562` writes `` ` · synced ${new Date(selected.lastSyncedAt * 1000).toLocaleString()}` `` — a raw locale string **with seconds** (`synced 7/30/2025, 2:30:00 AM`) that wraps `#library-summary` to two lines at 1120px. Spec §4.1 wants relative under a week, absolute beyond it, and never a raw `toLocaleString()`.

**`nowSeconds` is a required parameter, never `Date.now()` inside the formatter.** A formatter that reads the clock cannot be tested deterministically, and every one of these branches is a boundary worth asserting.

**I wrote and ran this implementation during planning; the outputs in Step 1 are observed.** Note `Date.UTC`/`getUTC*` throughout: mixing local getters with a UTC test fixture makes the year-boundary case pass or fail depending on the machine's timezone.

- [ ] **Step 1: Write the failing test**

Extend the import in `static/format.test.js` and append:

```js
import { AppError, errorCopy, formatPostedDate, formatSyncedAt, ordinal, sourceKindLabel } from "./format.js";
```

```js
test("posted dates are relative under a week and absolute beyond it", () => {
  // Fixed UTC instant so every boundary is deterministic: 30 Jul 2026, 12:00 UTC.
  const NOW = Date.UTC(2026, 6, 30, 12, 0, 0) / 1000;
  const DAY = 86400;

  // Never state a number you do not have (spec 4.7).
  assert.equal(formatPostedDate(0, NOW), "—");
  assert.equal(formatPostedDate(undefined, NOW), "—");

  assert.equal(formatPostedDate(NOW - 30, NOW), "Just now");
  assert.equal(formatPostedDate(NOW - 45 * 60, NOW), "45m ago");
  assert.equal(formatPostedDate(NOW - 2 * 3600, NOW), "2h ago");
  assert.equal(formatPostedDate(NOW - DAY, NOW), "Yesterday");
  assert.equal(formatPostedDate(NOW - 3 * DAY, NOW), "3d ago");

  // Both sides of the seven-day cutoff: relative just under, absolute exactly on it.
  assert.equal(formatPostedDate(NOW - (7 * DAY - 3600), NOW), "6d ago");
  assert.equal(formatPostedDate(NOW - 7 * DAY, NOW), "23 Jul");
  assert.equal(formatPostedDate(NOW - 8 * DAY, NOW), "22 Jul");

  // Across years the two-digit year appears, or "25 Jun" is ambiguous in a channel archive.
  assert.equal(formatPostedDate(NOW - 400 * DAY, NOW), "25 Jun 25");

  // A clock skew between server and browser must not print "-3m ago".
  assert.equal(formatPostedDate(NOW + 500, NOW), "Just now");
});

test("ordinals pad to the width of the total", () => {
  // The ordinal is the real play position, so it has to line up in a mono column.
  assert.equal(ordinal(7, 412), "007");
  assert.equal(ordinal(7, 9), "7");
  assert.equal(ordinal(1, 1000), "0001");
  assert.equal(ordinal(412, 412), "412");
  assert.equal(ordinal(0, 0), "0");
});

test("sync timestamps stop printing seconds", () => {
  const NOW = Date.UTC(2026, 6, 30, 12, 0, 0) / 1000;
  const DAY = 86400;
  assert.equal(formatSyncedAt(0, NOW), "");
  assert.equal(formatSyncedAt(NOW - 30, NOW), "synced just now");
  assert.equal(formatSyncedAt(NOW - 300, NOW), "synced 5m ago");
  assert.equal(formatSyncedAt(NOW - 2 * 3600, NOW), "synced 2h ago");
  assert.equal(formatSyncedAt(NOW - DAY, NOW), "synced Yesterday");
  assert.equal(formatSyncedAt(NOW - 9 * DAY, NOW), "synced 21 Jul");
  assert.equal(formatSyncedAt(NOW - 400 * DAY, NOW), "synced 25 Jun 25");
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test static/*.test.js`
Expected: FAIL with `SyntaxError: The requested module './format.js' does not provide an export named 'formatPostedDate'`

- [ ] **Step 3: Implement the three formatters**

Append to `static/format.js`:

```js
const DAY = 86400;
const WEEK = 7 * DAY;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// nowSeconds is a parameter, not Date.now(), so every boundary here is testable. UTC getters
// throughout: mixing local getters with a UTC caller makes the year boundary machine-dependent.
export function formatPostedDate(seconds, nowSeconds) {
  const posted = Number(seconds) || 0;
  if (posted <= 0) return "—";
  const now = Number(nowSeconds) || 0;
  const elapsed = now - posted;
  // Server and browser clocks disagree; a negative age must not render as "-3m ago".
  if (elapsed < 0) return "Just now";
  if (elapsed < 3600) { const minutes = Math.floor(elapsed / 60); return minutes < 1 ? "Just now" : `${minutes}m ago`; }
  if (elapsed < DAY) return `${Math.floor(elapsed / 3600)}h ago`;
  if (elapsed < WEEK) { const days = Math.floor(elapsed / DAY); return days === 1 ? "Yesterday" : `${days}d ago`; }
  const date = new Date(posted * 1000);
  const stem = `${date.getUTCDate()} ${MONTHS[date.getUTCMonth()]}`;
  return date.getUTCFullYear() === new Date(now * 1000).getUTCFullYear()
    ? stem : `${stem} ${String(date.getUTCFullYear()).slice(2)}`;
}

// Zero-padded to the width of the total so the mono column stays aligned: 007 of 412.
export function ordinal(position, total) {
  const width = String(Math.max(1, Number(total) || 1)).length;
  return String(Math.max(0, Number(position) || 0)).padStart(width, "0");
}

export function formatSyncedAt(seconds, nowSeconds) {
  const synced = Number(seconds) || 0;
  if (synced <= 0) return "";
  const elapsed = (Number(nowSeconds) || 0) - synced;
  if (elapsed < 60) return "synced just now";
  if (elapsed < 3600) return `synced ${Math.floor(elapsed / 60)}m ago`;
  if (elapsed < DAY) return `synced ${Math.floor(elapsed / 3600)}h ago`;
  return `synced ${formatPostedDate(synced, nowSeconds)}`;
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `node --test static/*.test.js`
Expected: PASS

- [ ] **Step 5: Use it in the library summary**

`static/app.js:562` — the raw locale string goes. `formatSyncedAt` returns `""` when there is nothing to report, so the separator is conditional on the string rather than on the field:

```js
  const synced = formatSyncedAt(selected?.lastSyncedAt, Math.floor(Date.now() / 1000));
  $("library-summary").textContent = `${state.totalTracks.toLocaleString()} ${state.totalTracks === 1 ? "track" : "tracks"}${synced ? ` · ${synced}` : ""}`;
```

`Date.now()` belongs here, at the call site, not inside the formatter.

- [ ] **Step 6: Verify**

Run: `node --test static/*.test.js`
Expected: PASS

Run: `./.venv/bin/python -m unittest discover -s tests`
Expected: PASS, `OK`

Check at 1120px with the panel open that `#library-summary` now fits on one line — the two-line wrap was the visible symptom.

- [ ] **Step 7: Commit**

```bash
git add static/format.js static/format.test.js static/app.js
git commit -m "Format posted and synced dates instead of dumping locale strings

The library summary printed 'synced 7/30/2025, 2:30:00 AM' -- a raw
toLocaleString() with seconds -- which wrapped the summary to two lines at 1120px.
Dates are now relative under a week and absolute beyond it, with a two-digit year
across years so '25 Jun' is not ambiguous in a channel archive.

nowSeconds is a parameter rather than a Date.now() call inside the formatter, so
both sides of the seven-day cutoff, the year boundary, and negative ages from
clock skew are all asserted. UTC getters throughout: local ones would make the
year-boundary test pass or fail depending on the machine's timezone.

ordinal() pads to the width of the total (007 of 412) for Task 18's mono column."
```

---

### Task 17: backend `sort`, with a server-side allowlist

**Files:**
- Modify: `core.py:934-991` (`list_tracks` gains a `sort` parameter; the `ORDER BY` at `core.py:963`), a module-level `_TRACK_SORTS`
- Modify: `app.py:667-682` (`/api/tracks` gains `sort`)
- Test: `tests/test_core.py` (extend the existing `CoreTests`)

**Interfaces:**
- Consumes: nothing
- Produces: `Database.list_tracks(..., sort: str = "posted")` and `GET /api/tracks?sort=`. Task 19 sends the parameter and adds it to the client cache key.

Audit D3: tracks come back `ORDER BY t.sent_at DESC, t.rowid DESC`, the date is never displayed, and there is no sort control — while a `TRACK / SOURCE / TIME` header sits above the list looking sortable. For a library assembled from channel posts, "when was this posted" is the most useful axis and the one the UI hides.

**Security — a standing constraint from the handoff.** The `sort` value must resolve through a **server-side allowlist** to a fixed SQL fragment. Never interpolate client input into SQL. An unknown value falls back to `posted` rather than erroring, so a stale bookmark or a hand-edited URL degrades instead of 500ing.

**I ran these four fragments against a real database during planning.** Observed results, which are what the tests assert:

| `sort` | Fragment | Verified behaviour |
|---|---|---|
| `posted` | `t.sent_at DESC` | unchanged from today |
| `title` | `COALESCE(NULLIF(json_extract(o.payload,'$.title'),''), NULLIF(t.telegram_title,''), t.file_name) COLLATE NOCASE ASC` | sorts by the **displayed** title: a track whose override is "Aardvark override" sorts first even though its Telegram title is "Zebra" |
| `artist` | `COALESCE(NULLIF(json_extract(o.payload,'$.artist'),''), NULLIF(t.telegram_artist,''), 'Unknown artist') COLLATE NOCASE ASC` | override-aware, same shape |
| `duration` | `t.duration_ms DESC` | longest first |

`COLLATE NOCASE` matters and is easy to get wrong: without it, `"apple"` sorts *after* `"Zebra"` by byte order, which looks broken in a list of mixed-case rips.

**A dead branch worth knowing about.** `upsert_tracks` writes `item.get("title") or "Unknown title"` at `core.py:781`, so `t.telegram_title` is **never empty or NULL** for an ingested track — the `t.file_name` arm of that COALESCE is unreachable in practice. Keep it as a defensive third arm (rows predating that behaviour, or a future ingest path), but do not write a test asserting the filename appears, because it cannot. This is exactly the kind of "obvious" test that would fail for a reason unrelated to the code under test.

Also: `t.rowid DESC` stays as the final tiebreak on **every** fragment. Two tracks with the same duration and no tiebreak means SQLite may order them differently between pages, and a row would appear twice or vanish while scrolling.

- [ ] **Step 1: Write the failing test**

Append to `class CoreTests` in `tests/test_core.py`:

```python
    def test_track_sort_uses_an_allowlist_and_matches_what_is_displayed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "library.sqlite3")
            database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
            database.upsert_tracks([
                {"chatId": "1", "messageId": "1", "fileName": "z.mp3", "mimeType": "audio/mpeg",
                 "title": "Zebra", "artist": "Beta", "durationMs": 300_000, "sentAt": 300},
                {"chatId": "1", "messageId": "2", "fileName": "a.mp3", "mimeType": "audio/mpeg",
                 "title": "apple", "artist": "Alpha", "durationMs": 100_000, "sentAt": 200},
                {"chatId": "1", "messageId": "3", "fileName": "m.mp3", "mimeType": "audio/mpeg",
                 "title": "Mango", "artist": "Gamma", "durationMs": 200_000, "sentAt": 100},
            ])
            # The displayed title wins over the Telegram one, so sorting must follow the override.
            database.save_metadata_patch("1", "1", {"title": "Aardvark override"}, [])

            def keys(sort):
                return [item["key"] for item in database.list_tracks(sort=sort)["items"]]

            self.assertEqual(["1:1", "1:2", "1:3"], keys("posted"))
            # Aardvark override first, then apple -- COLLATE NOCASE, or "apple" would follow "Mango".
            self.assertEqual(["1:1", "1:2", "1:3"], keys("title"))
            self.assertEqual(["1:2", "1:1", "1:3"], keys("artist"))
            self.assertEqual(["1:1", "1:3", "1:2"], keys("duration"))

            # Anything not on the allowlist degrades to posted rather than reaching SQL.
            for hostile in ["title; DROP TABLE tracks", "t.sent_at ASC", "", "nonsense", None]:
                self.assertEqual(keys("posted"), keys(hostile), f"{hostile!r} was not rejected")
            # And the table is still there.
            self.assertEqual(3, database.list_tracks()["total"])
            database.close()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./.venv/bin/python -m unittest tests.test_core.CoreTests.test_track_sort_uses_an_allowlist_and_matches_what_is_displayed -v`
Expected: FAIL with `TypeError: list_tracks() got an unexpected keyword argument 'sort'`

- [ ] **Step 3: Add the allowlist and the parameter**

Module level in `core.py`, near the other module constants:

```python
# The client sends a key, never SQL. An unknown key degrades to posted rather than erroring, so
# a stale bookmark or a hand-edited URL cannot 500. Every fragment keeps t.rowid DESC as its
# final tiebreak (added at the call site): without it, equal keys can order differently between
# pages and a row shows up twice or disappears while scrolling.
_TRACK_SORTS = {
    "posted": "t.sent_at DESC",
    "title": "COALESCE(NULLIF(json_extract(o.payload,'$.title'),''), NULLIF(t.telegram_title,''), t.file_name) COLLATE NOCASE ASC",
    "artist": "COALESCE(NULLIF(json_extract(o.payload,'$.artist'),''), NULLIF(t.telegram_artist,''), 'Unknown artist') COLLATE NOCASE ASC",
    "duration": "t.duration_ms DESC",
}
```

`core.py:934-943` — add the parameter to the signature:

```python
        total: int | None = None,
        sort: str = "posted",
    ) -> dict[str, Any]:
        offset = max(0, int(offset))
        limit = max(25, min(int(limit), 200))
        order = _TRACK_SORTS.get(sort or "posted", _TRACK_SORTS["posted"])
```

`core.py:963` — the `ORDER BY` becomes the looked-up fragment. It is an f-string, so the value **must** come from the dict, never from the argument:

```python
            ORDER BY {order}, t.rowid DESC
```

The `COUNT(*)` query below (`core.py:975-985`) has no `ORDER BY` and must not gain one — ordering a count is wasted work.

- [ ] **Step 4: Run the test to verify it passes**

Run: `./.venv/bin/python -m unittest tests.test_core.CoreTests.test_track_sort_uses_an_allowlist_and_matches_what_is_displayed -v`
Expected: PASS, `OK` with 1 test

- [ ] **Step 5: Expose it on the route**

`app.py:667-682` — one parameter through, no validation here because the allowlist is the validation:

```python
    @application.get("/api/tracks")
    def tracks(
        request: Request,
        source: str | None = None,
        q: str = "",
        offset: int = 0,
        limit: int = 100,
        liked: bool = False,
        temporary: bool = False,
        total: int | None = None,
        sort: str = "posted",
    ) -> dict[str, Any]:
        # ponytail: the client already knows the total after the first page of a given filter
        # and echoes it back, which lets us skip the COUNT(*) entirely while scrolling.
        return database(request).list_tracks(
            source, q[:200], offset, limit, liked, temporary,
            total if total is not None and total >= 0 else None, sort,
        )
```

Check the positional order against the signature — `list_tracks` takes `(chat_id, query, offset, limit, liked, include_unselected, total, sort)`, and `temporary` maps to `include_unselected`.

- [ ] **Step 6: Decide the second `ORDER BY` at `core.py:1287`**

There is an identical `ORDER BY t.sent_at DESC, t.rowid DESC` at `core.py:1287`, in the queue-building query that also LEFT JOINs `playback_history h`. **Leave it alone**, and record why: it builds the *playback queue*, and Play/Shuffle should enqueue in the library's natural posting order regardless of how the user is currently *viewing* the list. Coupling queue order to a display preference would mean re-sorting a playing queue when someone changes a column header. If a future task wants "play in the order I see", that is a deliberate product decision needing its own test, not a side effect of this one.

- [ ] **Step 7: Verify**

Run: `./.venv/bin/python -m unittest discover -s tests`
Expected: PASS, `OK` (one more test than before)

Run: `node --test static/*.test.js`
Expected: PASS (untouched by this task)

- [ ] **Step 8: Commit**

```bash
git add core.py app.py tests/test_core.py
git commit -m "Sort the library by posted, title, artist or duration

The list was ORDER BY sent_at DESC with the date never displayed and no control,
while a TRACK/SOURCE/TIME header sat above it looking sortable. For a library
built from channel posts, 'when was this posted' is the most useful axis and the
one the UI hid.

sort resolves through a server-side allowlist to a fixed fragment -- the client
sends a key, never SQL -- and an unknown key degrades to posted so a stale
bookmark cannot 500. Title and artist sort by the *displayed* value, so a local
metadata override sorts where the user sees it, and COLLATE NOCASE keeps 'apple'
next to 'Aardvark' instead of after 'Zebra'. Every fragment keeps rowid DESC as a
tiebreak, or equal keys can reorder between pages and a row duplicates mid-scroll.

The queue-building ORDER BY at core.py:1287 is deliberately left alone: enqueue
order should not follow a display preference."
```

---

### Task 18: the numbered, dated row system

**Files:**
- Modify: `static/app.js:632` (`trackRowHeight`), `:581-596` (`renderTrackRow`), `:598-600` (`renderTrackPlaceholder`)
- Modify: `static/index.html:194-196` (the column head)
- Modify: `static/style.css:469-475` (the shared grid), `:480-482`, `:912`, `:1017`, `:1018`, `:841-842`
- Test: `tests/test_layout.py`

**Interfaces:**
- Consumes: `ordinal(position, total)` and `formatPostedDate(seconds, nowSeconds)` from Task 16
- Produces: the row markup Task 20 reuses for search results, and `--row-height: 52px` / `--art-row: 40px` from Task 13

Spec §4.1 is the centrepiece: the list is ordered by `sent_at DESC` and the date is never shown, never labelled, and not sortable (audit D3). The ordinal is the **real play position**, so the numbering carries information rather than decorating. 52px rows with 40px art give ~13 rows per 900px viewport, up from 7.

**Four hardcoded `68px` values must change together.** I grepped these; missing one is the trap:

| Location | Declaration | Becomes |
|---|---|---|
| `style.css:482` | `.track-row { height: 68px }` | `var(--row-height)` |
| `style.css:912` | `.track-row { … height: 68px }` (860px block) | `var(--row-height)` |
| `style.css:1017` | `.track-row { contain-intrinsic-size: 68px }` | `var(--row-height)` |
| `style.css:1018` | `.track-placeholder { height: 68px }` | `var(--row-height)` |

`contain-intrinsic-size` is the dangerous one: it feeds `content-visibility: auto`, so a stale 68px there makes the browser reserve the wrong height for off-screen rows and the scrollbar drifts against the spacers — a bug that looks like the virtualiser is broken when it is not. (`68px` also appears at `:795`, `:796`, `:988`, `:989` for candidate covers and the collapsed rail — **leave those alone**, they are unrelated.)

**The virtualiser needs no arithmetic change, and I verified that.** With `rowHeight` 52 instead of 68, `start = floor(firstVisible / 40) * 40 - 40`, `end = start + 80`, and spacers of `start * rowHeight` and `(total - end) * rowHeight` still sum exactly to the full list height (checked at scrollTop 0, 500, 2000 and 10000 over 5001 tracks: 260,052px = 5001 × 52 in every case). The maths is height-agnostic; only the CSS constants drift.

**`.track-head` and `.track-row` share one grid** (`style.css:469-475`), so the head and the rows must gain the same two columns in the same edit, plus the responsive overrides at `:841-842` and `:912`.

- [ ] **Step 1: Write the failing test**

Add to `LayoutTests` in `tests/test_layout.py`:

```python
    def test_rows_are_numbered_dated_and_52px(self):
        page = self.page(1440, 900)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_selector(".track-row:not(.track-placeholder)")
        shape = page.evaluate("""() => {
          const row = document.querySelector('.track-row:not(.track-placeholder)');
          return {
            height: Math.round(row.getBoundingClientRect().height),
            ordinal: row.querySelector('.track-ordinal')?.textContent ?? null,
            posted: row.querySelector('.track-posted')?.textContent ?? null,
            intrinsic: getComputedStyle(row).containIntrinsicSize,
            art: Math.round(document.querySelector('.row-art').getBoundingClientRect().width),
          };
        }""")
        self.assertEqual(52, shape["height"])
        self.assertEqual("52px", shape["intrinsic"].split()[-1],
                         "contain-intrinsic-size drifted from the row height, so off-screen rows reserve the wrong space")
        self.assertEqual(40, shape["art"])
        self.assertEqual("01", shape["ordinal"], "the ordinal is the real play position, zero-padded to the total")
        self.assertTrue(shape["posted"], "rows must show when the track was posted")

    def test_source_column_only_appears_across_sources(self):
        page = self.page(1440, 900)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_selector(".track-row:not(.track-placeholder)")
        # All music: the source is the one thing distinguishing otherwise similar rows.
        self.assertTrue(page.evaluate("() => !!document.querySelector('.track-source')"))
        across = page.evaluate("() => getComputedStyle(document.querySelector('.track-source')).display")
        self.assertNotEqual("none", across)
        # Inside one source it repeats the h1 on every row and is the widest column (audit D5).
        page.evaluate("() => document.querySelector('.library').classList.add('single-source')")
        page.wait_for_timeout(80)
        within = page.evaluate("() => getComputedStyle(document.querySelector('.track-source')).display")
        self.assertEqual("none", within, "the source column repeats the page title inside a single source")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./.venv/bin/python -m unittest tests.test_layout -v`
Expected: FAIL with `AssertionError: 52 != 68`

- [ ] **Step 3: Rebuild the row and the placeholder together**

`static/app.js:632` — the constant the virtualiser reads:

```js
function trackRowHeight() { return 52; }
```

`static/app.js:581-596` — `renderTrackRow` gains the ordinal and POSTED columns. The row's index in the library is its real play position, so pass it in; `renderTracks` already knows it as `start + offset`:

```js
function renderTrackRow(track, position = 0) {
  const playing = track.key === state.current?.key;
  const liked = Boolean(track.liked);
  const now = Math.floor(Date.now() / 1000);
  return `<article class="track-row ${playing ? "current" : ""}" data-track-key="${escapeHtml(track.key)}" tabindex="-1">
    <span class="track-ordinal utility">${playing ? `<span class="playing-mark" aria-label="Now playing"></span>` : ordinal(position + 1, state.totalTracks)}</span>
    <button class="track-main" type="button" data-play-key="${escapeHtml(track.key)}">
      <span class="mini-art-wrap"><img class="mini-art row-art" data-src="${mediaUrl(track)}?v=${encodeURIComponent(track.artworkVersion || "telegram")}" alt=""><span class="art-placeholder mini"><span></span></span><span class="track-play-overlay">${icon(playing && !audio.paused ? "pause" : "play-filled")}</span></span>
      <span class="track-copy"><strong>${escapeHtml(track.title)}</strong><small>${escapeHtml(track.artist || "Unknown artist")}</small></span>
    </button>
    <span class="track-source">${escapeHtml(track.source.title)}</span>
    <span class="track-posted utility">${escapeHtml(formatPostedDate(track.sentAt, now))}</span>
    <span class="track-duration utility">${formatTime(track.durationMs / 1000)}</span>
    <span class="track-row-actions">
      <button class="icon-button row-like ${liked ? "active" : ""}" type="button" data-row-like-key="${escapeHtml(track.key)}" aria-pressed="${liked}" aria-label="${liked ? "Unlike" : "Like"} ${escapeHtml(track.title)}">${icon(liked ? "heart-filled" : "heart")}</button>
    </span>
    <button class="icon-button row-menu" type="button" data-track-menu="${escapeHtml(track.key)}" aria-label="Actions for ${escapeHtml(track.title)}">${icon("more")}</button>
  </article>`;
}
```

`static/app.js:598-600` — the placeholder must carry the **same column count**, or the skeleton misaligns against real rows:

```js
function renderTrackPlaceholder() {
  // Seven top-level children, matching the seven grid columns exactly: ordinal, main, source,
  // posted, time, actions, menu. One short and the skeleton shears against real rows.
  return '<article class="track-row track-placeholder" aria-hidden="true"><i class="placeholder-ordinal"></i><span class="placeholder-main"><i></i><span><i></i><i></i></span></span><i class="placeholder-source"></i><i class="placeholder-posted"></i><i class="placeholder-time"></i><i></i><i></i></article>';
}
```

**Count the children before moving on.** The grid has 7 columns, `renderTrackRow` emits 7 top-level elements (`.track-ordinal`, `.track-main`, `.track-source`, `.track-posted`, `.track-duration`, `.track-row-actions`, `.row-menu`), and this placeholder must emit 7 as well — the two trailing bare `<i>` elements stand in for the actions and menu cells. My first draft of this markup had only 6 and would have shifted every skeleton cell one column left.

In `renderTracks` (`app.js:651-656`), pass the position — this is what makes the ordinal a real play position rather than a row counter:

```js
    const rows = Array.from({ length: end - start }, (_, offset) => state.tracks[start + offset] ? renderTrackRow(state.tracks[start + offset], start + offset) : renderTrackPlaceholder()).join("");
```

Import the two formatters by extending Task 4's line:

```js
import { AppError, errorCopy, formatPostedDate, formatSyncedAt, ordinal, sourceKindLabel } from "./format.js";
```

- [ ] **Step 4: Update the shared grid and the column head**

`static/style.css:469-475` — both selectors, one declaration. The ordinal is a fixed mono column; POSTED is sized for "29 Jul 25":

```css
.track-head,
.track-row {
  display: grid;
  grid-template-columns: 44px minmax(200px, 1.5fr) minmax(120px, .7fr) 76px 64px 36px 46px;
  align-items: center;
  column-gap: 14px;
}
```

`static/style.css:480-482`:

```css
.track-row {
  width: 100%;
  height: var(--row-height);
```

`static/style.css:1017-1018` — the two that break scroll geometry if forgotten:

```css
.track-row { content-visibility: auto; contain-intrinsic-size: var(--row-height); cursor: default; transition: background-color var(--dur-1) var(--ease-out), box-shadow var(--dur-1) var(--ease-out); }
.track-placeholder { position: relative; height: var(--row-height); overflow: hidden; pointer-events: none; }
```

`static/style.css` — art shrinks to the token, and the new columns get their type:

```css
.row-art { width: var(--art-row); height: var(--art-row); flex: 0 0 auto; border: 1px solid var(--rule); border-radius: 3px; background: var(--surface); object-fit: cover; }
.track-ordinal, .track-posted { color: var(--graphite); font-family: var(--font-mono); font-size: var(--text-data); font-variant-numeric: tabular-nums; }
.track-ordinal { text-align: right; }
/* The playing row's ordinal becomes the one stamped mark on screen. */
.playing-mark { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--stamp); }
/* Inside a single source the column prints the h1 on every row, and it is the widest one. */
.library.single-source .track-source { display: none; }
```

`static/index.html:194-196` — the head gains matching cells and stops claiming to be sortable furniture over nothing:

```html
        <div class="track-head utility" aria-hidden="true">
          <span>#</span><span>Track</span><span>Source</span><span>Posted</span><span>Time</span><span></span><span></span>
        </div>
```

`static/style.css:841-842` and `:912` — the responsive overrides need the same column count. At ≤1120px the source column already drops; keep POSTED, since the date is the axis this redesign is surfacing:

```css
  .track-head, .track-row { grid-template-columns: 40px minmax(190px, 1fr) 72px 64px 36px 44px; }
  .track-head span:nth-child(3), .track-source { display: none; }
```

```css
  .track-row { grid-template-columns: 34px minmax(0, 1fr) 64px 36px 36px; height: var(--row-height); padding: 7px 3px; column-gap: 8px; }
```

`renderSources` must toggle the class (`app.js:541-578`), beside where it already sets `#source-title`:

```js
  $("library").classList.toggle("single-source", Boolean(state.source) && !state.likedMode);
```

- [ ] **Step 5: Run the tests**

Run: `./.venv/bin/python -m unittest tests.test_layout -v`
Expected: PASS

Run: `./.venv/bin/python -m unittest discover -s tests` and `node --test static/*.test.js`
Expected: PASS, `OK`

- [ ] **Step 6: Scroll a large library, because this is where virtualisation breaks**

The unit assertions cannot catch spacer drift. With the seeded 5001-track fixture, scroll to the middle and to the end and confirm: no row renders twice, no gap opens between the last row and the bottom of the scroller, and the scrollbar thumb reaches the bottom exactly when the final track does. Then reload at a scrolled position — `firstVisible` is computed from `scroller.scrollTop - list.offsetTop`, and the taller header changes `offsetTop`.

- [ ] **Step 7: Commit**

```bash
git add static/app.js static/index.html static/style.css tests/test_layout.py
git commit -m "Number and date the library rows

The list was ordered by sent_at DESC with the date shown nowhere, and the widest
column repeated the page title on every row inside a single source. Rows now lead
with the real play position (zero-padded to the total, so 007 of 412) and carry a
POSTED column; SOURCE appears only in All music and Liked, where it distinguishes
something.

52px rows with 40px art fit ~13 per 900px viewport instead of 7. The height lived
in four places -- height, the 860px override, contain-intrinsic-size and
.track-placeholder -- and all four are now var(--row-height). The
contain-intrinsic-size one matters most: stale, it makes content-visibility
reserve the wrong height for off-screen rows and the scrollbar drifts against the
virtualiser's spacers, which looks like a virtualiser bug and is not.

The window arithmetic itself is unchanged and height-agnostic: spacers plus
rendered rows still sum to total * rowHeight at every scroll position."
```

---

### Task 19: the sort control

**Files:**
- Modify: `static/index.html` (a sort control in `.header-actions`; the `.track-head` cells become buttons)
- Modify: `static/app.js:672-676` (`libraryParameters` — **the cache key**), `static/app.js` (state + handler), `static/app.js:634-670` (mark the active column)
- Modify: `static/style.css` (the head buttons and the active marker)
- Test: `tests/test_layout.py`

**Interfaces:**
- Consumes: `GET /api/tracks?sort=` from Task 17; the 7-column grid from Task 18
- Produces: `state.sort` (default `"posted"`), and `sort` inside the library cache key

**The trap, stated plainly.** `libraryParameters(offset)` (`app.js:672-676`) returns the query string **and doubles as the `state.libraryCache` key**. Its current value is exactly:

```
source=…&q=…&offset=…&limit=100&liked=…&temporary=…
```

If `sort` is added to the request but not to this string, every sort change hits a cache entry keyed identically to the previous sort and **serves the old rows** — the control appears to do nothing, intermittently, depending on what is cached. One function, both jobs, so adding the parameter here fixes the request and the key together.

- [ ] **Step 1: Write the failing test**

```python
    def test_sort_control_changes_the_requested_order(self):
        page = self.page(1440, 900)
        requested = []
        def record(route):
            requested.append(route.request().url)
            self._stub(route)
        page.route("**/api/tracks*", record)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_selector(".track-row:not(.track-placeholder)")
        self.assertTrue(any("sort=posted" in url for url in requested),
                        f"the default sort is not sent: {requested}")

        requested.clear()
        page.select_option("#track-sort", "title")
        page.wait_for_function("() => document.querySelectorAll('.track-row').length > 0")
        # It must reach the network, not a cache entry keyed without sort.
        self.assertTrue(any("sort=title" in url for url in requested),
                        f"changing sort served a stale cache entry instead of refetching: {requested}")

        marked = page.evaluate("""() => [...document.querySelectorAll('.track-head [aria-sort]')]
          .map((cell) => [cell.textContent.trim(), cell.getAttribute('aria-sort')])""")
        self.assertIn(["Track", "ascending"], marked,
                      f"the active sort key is not marked in the column header: {marked}")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./.venv/bin/python -m unittest tests.test_layout -v`
Expected: FAIL with a Playwright timeout on `#track-sort` — the control does not exist.

- [ ] **Step 3: Add the control and thread `sort` through the cache key**

`static/index.html` — in `.header-actions`, after the `.search-control` label. A native `<select>` because spec §5 forbids a component framework and this is exactly what the element is for — it is keyboard-accessible and mobile-native for free:

```html
            <label class="sort-control">
              <span class="sr-only">Sort tracks</span>
              <select id="track-sort">
                <option value="posted" selected>Posted</option>
                <option value="title">Title</option>
                <option value="artist">Artist</option>
                <option value="duration">Duration</option>
              </select>
            </label>
```

`static/app.js` — add to the `state` object beside `source` and `likedMode`:

```js
  sort: "posted",
```

`static/app.js:672-676` — **the one edit that matters**. `sort` joins the string that is both the query and the cache key:

```js
function libraryParameters(offset) {
  const query = $("track-search").value.trim();
  const temporary = Boolean(state.temporarySource?.chatId === state.source && !state.sources.some((item) => item.chatId === state.source));
  // This string is also the state.libraryCache key. Anything that changes which rows come back
  // must appear here, or a sort change is served the previous sort's cached page.
  return `source=${encodeURIComponent(state.likedMode ? "" : state.source)}&q=${encodeURIComponent(query)}&offset=${offset}&limit=100&liked=${state.likedMode}&temporary=${temporary}&sort=${encodeURIComponent(state.sort)}`;
}
```

The handler, beside the other listeners. `loadLibrary(true)` is the existing full-reload path:

```js
$("track-sort").addEventListener("change", (event) => {
  state.sort = event.target.value;
  state.libraryCache.clear();
  loadLibrary(true).catch(showError);
});
```

Clearing the cache is belt-and-braces: the key already differs, but stale entries for the old key would otherwise sit there until eviction.

- [ ] **Step 4: Mark the active key in the column header**

The head is `aria-hidden="true"` today, which is right while it is decorative — but a *sortable* header must be reachable, so drop that attribute and make the two sortable cells buttons. Spec §4.1: "with the active key marked in the column header".

`static/index.html:194-196`:

```html
        <div class="track-head utility">
          <span>#</span>
          <button type="button" class="head-sort" data-sort="title">Track</button>
          <span>Source</span>
          <button type="button" class="head-sort" data-sort="posted">Posted</button>
          <button type="button" class="head-sort" data-sort="duration">Time</button>
          <span></span><span></span>
        </div>
```

In `renderTracks`, reflect state onto both the head and the select so the two controls can never disagree:

```js
  for (const cell of document.querySelectorAll(".track-head [data-sort]")) {
    const active = cell.dataset.sort === state.sort;
    // Posted and Duration read newest/longest first; Title and Artist read A-Z.
    if (active) cell.setAttribute("aria-sort", state.sort === "title" || state.sort === "artist" ? "ascending" : "descending");
    else cell.removeAttribute("aria-sort");
  }
  $("track-sort").value = state.sort;
```

And route header clicks through the same path as the select:

```js
document.querySelector(".track-head").addEventListener("click", (event) => {
  const cell = event.target.closest("[data-sort]");
  if (!cell) return;
  $("track-sort").value = cell.dataset.sort;
  $("track-sort").dispatchEvent(new Event("change"));
});
```

`static/style.css` — the head buttons must not look like buttons, only behave like them:

```css
.head-sort { padding: 0; border: 0; background: none; color: inherit; font: inherit; letter-spacing: inherit; text-align: inherit; text-transform: inherit; cursor: pointer; }
.head-sort:hover { color: var(--ink); }
.head-sort[aria-sort] { color: var(--ink); font-weight: 620; }
.head-sort[aria-sort]::after { margin-left: 4px; content: "↓"; }
.head-sort[aria-sort="ascending"]::after { content: "↑"; }
.sort-control select { min-height: 40px; }
```

- [ ] **Step 5: Run the tests**

Run: `./.venv/bin/python -m unittest tests.test_layout -v`
Expected: PASS

Run: `./.venv/bin/python -m unittest discover -s tests` and `node --test static/*.test.js`
Expected: PASS, `OK`

- [ ] **Step 6: Exercise the cache path by hand, because that is the failure mode**

Sort by Title, then Posted, then **back to Title**. The third change is the one that reveals a bad key: it is the first request for a filter combination already in the cache. Rows must reorder every time. Then set a filter, sort, and clear the filter — `q` and `sort` are both in the key, so all four combinations must be distinct.

- [ ] **Step 7: Commit**

```bash
git add static/index.html static/app.js static/style.css tests/test_layout.py
git commit -m "Add a sort control for the track list

Posted (default), Title, Artist and Duration, driven by the server-side allowlist
from the previous commit. The active key is marked in the column header, and the
header cells that map to a sort are now buttons rather than aria-hidden
decoration -- a header that claims to be sortable should be reachable.

The load-bearing change is one line: sort joins libraryParameters(), which is both
the query string and the state.libraryCache key. Added to the request but not the
key, a sort change would be served the previous sort's cached page and the control
would appear to do nothing, intermittently, depending on what was cached."
```

---

### Task 20: unified search-result rows

**Files:**
- Modify: `static/app.js:795-800` (both result templates), `static/app.js:818-819` (capture the cap)
- Modify: `static/style.css:313-332` (`.global-result*`), delete `.global-result-mark` at `:333`
- Test: `tests/test_layout.py`

**Interfaces:**
- Consumes: `sourceKindLabel` (Task 4), the row metrics from Task 18
- Produces: nothing later tasks depend on

Two findings, both verified in the source:

**D6 — one 8px column carries four unrelated meanings.** `.global-result-mark` (`style.css:333`, `font-size: 8px`) renders `"Open"` or `"Preview"` for sources (`app.js:796`), and a formatted duration **or** the literal word `"Telegram"` for tracks (`app.js:799`). Provenance and duration in the same slot, at 8px.

**D8 — search results are a second, weaker row system.** Measured from the CSS: `.global-result` is a 2-column grid (`minmax(0, 1fr) auto`) with `min-height: 52px`, **no artwork at all**, `strong` at 13px and `small` at 10px — against library rows with 7 columns, 40px art, and `--text-body`. Same objects, two treatments, and recognition is weakest exactly where it matters most.

**The cap is real and silent.** `TelegramSearchBody` defaults `limit` to **30** (max 50) at `app.py:218`, and `global_music_search` clamps to the same at `telegram_service.py:585`. Nothing in the UI says the list is truncated, so "my track isn't here" is indistinguishable from "there are only 30 results". Spec §4.6 wants a result count; say when it is capped.

Spec §4.6: one row system — same artwork, same metrics, same type sizes, **ordinal-free** (a search result has no play position) — with the 8px mark split into an explicit `--text-micro` provenance tag ("In your library" / "On Telegram") plus a duration in the normal duration column.

- [ ] **Step 1: Write the failing test**

```python
    def test_search_results_reuse_the_library_row_system(self):
        page = self.page(1440, 900)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.fill("#global-search", "burial")
        page.wait_for_selector(".global-result")
        shape = page.evaluate("""() => {
          const row = document.querySelector('.global-result');
          const art = row.querySelector('.row-art');
          return {
            art: art ? Math.round(art.getBoundingClientRect().width) : 0,
            titlePx: getComputedStyle(row.querySelector('strong')).fontSize,
            provenance: row.querySelector('.result-provenance')?.textContent.trim() ?? null,
            hasDuration: !!row.querySelector('.track-duration'),
            marks: document.querySelectorAll('.global-result-mark').length,
          };
        }""")
        self.assertEqual(40, shape["art"], "search results have no artwork, so recognition is weakest where it matters most")
        self.assertEqual(0, shape["marks"], "the four-meaning 8px mark column still exists")
        self.assertIn(shape["provenance"], ("In your library", "On Telegram"))
        self.assertTrue(shape["hasDuration"], "duration belongs in the duration column")
        self.assertEqual("15px", shape["titlePx"], "result titles should match library rows (--text-body)")
        count = page.text_content("#global-results-count")
        self.assertRegex(count, r"\d+ result", f"no result count shown: {count!r}")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./.venv/bin/python -m unittest tests.test_layout -v`
Expected: FAIL with `AssertionError: 0 != 40` — there is no artwork in a search result.

- [ ] **Step 3: Rebuild both result templates on the library row's shape**

`static/app.js:795-800`. Sources keep their own affordance (they are not tracks, so no duration), but the kind label goes through the formatter and the mark becomes an explicit tag:

```js
  $("global-source-results").innerHTML = state.globalSources.length
    ? `<h3>Telegram sources</h3>${state.globalSources.map((source) => `<button class="global-result" type="button" data-global-source="${source.chatId}"><span class="result-art-wrap">${avatarMarkup(source)}</span><span class="track-copy"><strong>${escapeHtml(source.title)}</strong><small>${escapeHtml(sourceKindLabel(source.kind))}${source.trackCount ? ` · ${source.trackCount.toLocaleString()} known tracks` : ""}</small></span><span class="result-provenance">${source.selected ? "In your library" : "On Telegram"}</span><span class="track-duration utility"></span></button>`).join("")}`
    : "";
  $("global-track-results").innerHTML = state.globalTracks.length
    ? `<h3>Tracks</h3>${state.globalTracks.map((track) => `<button class="global-result" type="button" data-global-track="${escapeHtml(track.key)}"><span class="result-art-wrap"><img class="row-art" src="${mediaUrl(track)}?v=${encodeURIComponent(track.artworkVersion || "telegram")}" alt="" loading="lazy"></span><span class="track-copy"><strong>${escapeHtml(track.title)}</strong><small>${escapeHtml(track.artist || "Unknown artist")} · ${escapeHtml(track.source.title)}</small></span><span class="result-provenance">${track.source.selected ? "In your library" : "On Telegram"}</span><span class="track-duration utility">${formatTime(track.durationMs / 1000)}</span></button>`).join("")}`
    : "";
```

Note `src` rather than `data-src`: the lazy `coverObserver` (`app.js:612-621`) is scoped to the library scroller via `root: $("library")`, so results in the dropdown would never intersect it and their covers would never load. `loading="lazy"` covers it instead.

The count, and the cap. `#global-results-title` already exists (`index.html:129`); add a sibling `<span id="global-results-count" class="small-copy"></span>` and fill it where the results land (`app.js:818-819`):

```js
    state.globalTracks = Array.isArray(remote?.tracks) ? remote.tracks : [];
    state.globalSources = Array.isArray(remote?.sources) ? remote.sources : [];
    // The server caps at 30 (app.py:218). Saying so distinguishes "not found" from "not shown".
    const found = state.globalTracks.length + state.globalSources.length;
    $("global-results-count").textContent = found === GLOBAL_SEARCH_LIMIT
      ? `First ${found} results`
      : `${found} ${found === 1 ? "result" : "results"}`;
```

with `const GLOBAL_SEARCH_LIMIT = 30;` beside the other module constants, matching the server default.

- [ ] **Step 4: One row system in CSS**

`static/style.css:313-333` — the grid gains art and the two real columns, the type matches library rows, and the 8px mark is deleted:

```css
.global-result-group:empty { display: none; }
.global-result-group h3 { margin: 14px 0 5px; color: var(--graphite); font-family: var(--font-mono); font-size: var(--text-micro); font-weight: 500; letter-spacing: .08em; text-transform: uppercase; }
.global-result {
  display: grid;
  width: 100%;
  min-height: var(--row-height);
  grid-template-columns: var(--art-row) minmax(0, 1fr) auto 64px;
  align-items: center;
  gap: 12px;
  padding: 7px 5px;
  border: 0;
  border-bottom: 1px solid var(--rule-soft);
  background: transparent;
  cursor: pointer;
  text-align: left;
}
.global-result:hover, .global-result:focus-visible { background: var(--surface); }
.global-result strong, .global-result small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.global-result strong { font-size: var(--text-body); }
.global-result small { margin-top: 3px; color: var(--graphite); font-size: var(--text-small); }
.result-art-wrap { width: var(--art-row); height: var(--art-row); }
/* Provenance was one of four things an 8px column could mean. Now it says which it is. */
.result-provenance { color: var(--graphite); font-size: var(--text-micro); white-space: nowrap; }
```

`:hover` moves from `var(--paper)` to `var(--surface)`: after Task 13, `--paper` *is* the dropdown's background, so the old value would make hover invisible.

- [ ] **Step 5: Run the tests**

Run: `./.venv/bin/python -m unittest tests.test_layout -v`
Expected: PASS

Run: `./.venv/bin/python -m unittest discover -s tests` and `node --test static/*.test.js`
Expected: PASS, `OK` — the token test's 11px floor stays green now that the 8px and 9px rules here are gone.

- [ ] **Step 6: Verify both branches by hand**

Search something that matches **both** a library track and a Telegram-only one, so both provenance strings render in one list. Confirm covers actually appear (the `data-src`/observer trap above is silent — a broken cover looks like "no artwork" rather than an error), and that a query returning exactly 30 results says "First 30 results".

- [ ] **Step 7: Commit**

```bash
git add static/app.js static/style.css tests/test_layout.py
git commit -m "Search results reuse the library row system

Global results were a second, weaker row system: a 2-column grid with no artwork
at all, 13px titles against the library's 15px, and one 8px column that meant
'Open', 'Preview', a duration, or the literal word 'Telegram' depending on which
branch built it. Same objects, two treatments, with recognition weakest exactly
where it matters most.

Now one row system -- same 40px art, same metrics, ordinal-free since a result has
no play position -- with provenance stated outright ('In your library' / 'On
Telegram') and duration in the duration column. Covers use src with loading=lazy,
not data-src: the lazy observer is rooted on the library scroller, so results in
the dropdown never intersect it and would never load.

The 30-result cap is now visible too; 'First 30 results' distinguishes 'not found'
from 'not shown'."
```

---

### Task 21: the Details tab gains its missing details

**Files:**
- Modify: `static/app.js:913` (`detailRows`)
- Modify: `static/index.html:242-249` (actions above the `<dl>`)
- Modify: `static/style.css:643` (`.detail-actions`)
- Test: `tests/test_layout.py`

**Interfaces:**
- Consumes: `formatPostedDate` (Task 16), `formatTime` (existing, from `player-core.js`)
- Produces: nothing

Audit D4: the tab renders Source, Album, Album artist, Genre, Year, File, Size — and omits duration, date posted, track number, disc number and format, **all of which are already returned**. Track and disc number are editable in the metadata dialog yet never displayed anywhere, which is the odd part.

**No backend change is needed, and it is worth being precise about why.** The Details tab is fed by `_track_row` (`core.py:1057-1096`), the *single-track* shape, which is richer than the `_track_summary` shape used for lists. It already returns `durationMs`, `sentAt`, `file.mimeType`, `file.size`, and `metadata.trackNumber` / `metadata.discNumber` (merged from `telegramMetadata` and `overrides` at `core.py:1069-1070`). Spec §5 forbids schema changes; none is required.

**Actions above the fold.** `.detail-actions` currently sits *after* the `<dl>` (`index.html:243-248`) with `margin-top: 24px` (`style.css:643`), so with the old 546px header the buttons were pushed off-screen entirely. Task 2 fixed the header; moving the actions above the list is the other half, and it is what spec §4.3 asks for.

Keep the comment at `app.js:914-915` — it records that removing the speed control once left this `<dl>` empty, which is exactly the kind of history that stops someone "tidying" the rows away again.

- [ ] **Step 1: Write the failing test**

```python
    def test_details_tab_shows_everything_already_indexed(self):
        page = self.page(1440, 900)
        self.open_now_panel(page)
        page.evaluate("""() => {
          document.getElementById('details-pane').hidden = false;
          document.getElementById('track-details').innerHTML = '';
        }""")
        # Drive the real render path rather than asserting on hand-written markup.
        page.evaluate("""() => window.__renderDetailsForTest({
          key: '-1001:1000', source: { title: 'Hyperdub' },
          metadata: { album: 'Rival Dealer', albumArtist: 'Burial', genre: 'Bass',
                      year: 2013, trackNumber: 2, discNumber: 1 },
          file: { name: 'rival-dealer.mp3', mimeType: 'audio/mpeg', size: 14_680_064 },
          durationMs: 602_000, sentAt: 1753800000,
        })""")
        labels = page.evaluate("() => [...document.querySelectorAll('#track-details dt')].map((d) => d.textContent)")
        for expected in ["Duration", "Posted", "Track", "Disc", "Format"]:
            self.assertIn(expected, labels, f"{expected} is indexed but not shown: {labels}")
        values = page.evaluate("() => [...document.querySelectorAll('#track-details dd')].map((d) => d.textContent)")
        self.assertIn("10:02", values, f"duration not formatted: {values}")

        # Actions must be reachable without scrolling the pane: DOCUMENT_POSITION_FOLLOWING (4)
        # means the list comes after the actions.
        position = page.evaluate("""() => document.querySelector('.detail-actions')
          .compareDocumentPosition(document.getElementById('track-details'))""")
        self.assertEqual(4, position & 4, "the action buttons must precede the detail list")
```

Expose the render path for the test beside `setTrackUi` (this is the smallest seam; the alternative is asserting on markup the app did not build):

```js
window.__renderDetailsForTest = (track) => { $("track-details").innerHTML = detailRowsFor(track); };
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./.venv/bin/python -m unittest tests.test_layout -v`
Expected: FAIL with `AssertionError: 'Duration' is indexed but not shown: ['Source', 'Album', 'Album artist', 'Genre', 'File', 'Size']`

- [ ] **Step 3: Extract the rows and add the five missing fields**

`static/app.js:913` — pull the expression into a named function so it is testable, then extend it. Order matters: identity first, then the numbers, then the file:

```js
function detailRowsFor(track) {
  const metadata = track.metadata || {};
  const now = Math.floor(Date.now() / 1000);
  const disc = Number(metadata.discNumber) || 0;
  const number = Number(metadata.trackNumber) || 0;
  return [
    ["Source", track.source.title],
    ["Album", metadata.album],
    ["Album artist", metadata.albumArtist],
    ["Genre", metadata.genre],
    ["Year", metadata.year || ""],
    ["Duration", track.durationMs ? formatTime(track.durationMs / 1000) : ""],
    ["Posted", track.sentAt ? formatPostedDate(track.sentAt, now) : ""],
    ["Track", number ? String(number) : ""],
    ["Disc", disc ? String(disc) : ""],
    ["Format", (track.file?.mimeType || "").replace(/^audio\//, "").toUpperCase()],
    ["File", track.file?.name],
    ["Size", track.file?.size ? `${(track.file.size / 1048576).toFixed(1)} MB` : ""],
  ].filter(([, value]) => value).map(([key, value]) => `<div><dt>${key}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");
}
```

`audio/mpeg` → `MPEG` is more use than the raw mime type, and the existing `.filter(([, value]) => value)` keeps every row absent when its data is — so a track with no disc number shows no Disc row rather than "0", which is the spec §4.7 rule about never stating a number you do not have.

At the call site (`app.js:913-917`), keep the existing comment:

```js
  const detailRows = detailRowsFor(track);
  // Removing the speed control took the only write of detailRows with it, so the Details tab
  // rendered its action buttons over an empty <dl>. The rows are the tab's actual content.
  $("track-details").innerHTML = detailRows;
```

- [ ] **Step 4: Put the actions above the fold**

`static/index.html:242-249` — swap the order inside `#details-pane`:

```html
          <div id="details-pane" class="details-pane" role="tabpanel" hidden>
            <div class="detail-actions">
              <button id="edit-current" class="button" type="button"><svg><use href="#i-edit"/></svg>Edit metadata</button>
              <a id="download-current" class="button" href="#"><svg><use href="#i-download"/></svg>Download</a>
              <button id="edit-lyrics" class="button" type="button"><svg><use href="#i-lyrics"/></svg>Edit lyrics</button>
            </div>
            <dl id="track-details"></dl>
          </div>
```

`static/style.css:643` — the margin moves to the bottom, since the block moved to the top:

```css
.detail-actions { display: grid; gap: 8px; margin-bottom: 24px; }
```

The `<dl>` grows by five rows, so this is also what stops the actions receding as the data improves.

- [ ] **Step 5: Run the tests**

Run: `./.venv/bin/python -m unittest tests.test_layout -v`
Expected: PASS

Run: `./.venv/bin/python -m unittest discover -s tests` and `node --test static/*.test.js`
Expected: PASS, `OK`

- [ ] **Step 6: Check a sparse track, not just a rich one**

Open Details for a file with no album, no year and no disc number — the common case in this library. Confirm those rows are **absent** rather than showing "0" or an empty `<dd>`, and that the pane still reads as a list rather than three buttons over one row.

- [ ] **Step 7: Commit**

```bash
git add static/app.js static/index.html static/style.css tests/test_layout.py
git commit -m "Details tab shows the fields it already had

Duration, date posted, track number, disc number and format were all indexed and
returned by _track_row -- track and disc number are even editable in the metadata
dialog -- and none of them were displayed. No backend change was needed; the
single-track shape already carried every one.

The action buttons also move above the list. They sat after a <dl> that is now
five rows longer, which is how they ended up below the fold behind the 546px
header in the first place. Empty fields stay absent rather than rendering '0'."
```

---

### Task 22: the copy pass

**Files:**
- Modify: `static/index.html` (eyebrows at `:27 :44 :129 :301 :311 :339 :354 :367`; `#bind-host-help` at `:400`)
- Modify: `static/app.js:803` (empty state), `:1559` (contact count), `:1560` ("Telegram contact"), `:1664` and `:2083` (`N cached`), `:1697` (the shell command), `:641-644` (headline punctuation), `:1183` (discover counts)
- Test: greps below, plus `tests/test_layout.py` for the rendered strings

**Interfaces:**
- Consumes: nothing
- Produces: nothing

Spec §4.7 in full. Copy is mostly not unit-testable, so the check is a **grep asserting each deleted string is gone from both files** — and note that many of these live in `app.js`, not the markup, so a grep over `index.html` alone would report success while the string still ships.

**Eyebrows.** Ten panels carry one and several restate their own heading. Keep only the two that classify: `#source-kind` (`index.html:169`, which Task 4 feeds `sourceKindLabel`) and `SAVED LOCALLY` over Liked songs. Delete the eyebrow lines at `index.html:27` (`Locked`), `:44` (`One-time connection`), `:129` (`Library lens`), `:301` (`Telegram library`), `:311` (`Local metadata layer`), `:339` (`Plain or LRC`), `:354` (`Telegram contacts`), `:367` (`Player preferences`). `LIBRARY LENS` and `LOCAL METADATA LAYER` are implementation language — nobody has a lens.

**Verified strings to change:**

| Where | Now | Becomes |
|---|---|---|
| `app.js:803` | `"No matches yet."` | `"Nothing matches that search"` — *yet* implies more is coming, and headlines take no terminal period |
| `app.js:1559` | `` `${contacts.length} matching contacts` `` | show nothing until a query is typed; `5 matching contacts` before you type is a lie |
| `app.js:1560` | `"Telegram contact"` for Saved Messages | `"Your cloud storage"` — it is not a contact, and the same destination is already a separate player button |
| `app.js:1664`, `:2083` | `` `${cache.files} cached` `` | `` `${cache.files} songs cached` `` — `41 cached` has no noun. **Two sites; the second is easy to miss** |
| `app.js:1697` | `"Started without run.py, so this setting is not applied. Restart with: uv run python run.py"` | `"This setting needs a restart to take effect."` — no shell commands in an end-user preference pane |
| `index.html:400` | `"Loopback keeps the player reachable only from this computer."` | explain the **risky** option too: `"This machine only keeps the player private. Anyone on my network lets other devices reach it — including anyone else on the same Wi-Fi."` |
| `app.js:1183` | `` `${item.musicFileCount ?? item.trackCount ?? "…"} music files` `` | `—` when uncounted, so an unscanned source stops looking identical to an empty one (audit D9) |
| `app.js:641-644` | `` `Nothing matches "${query}."` `` / `"Add a channel, bot, or private chat."` | drop the terminal periods |

**Preserve the filter-no-match empty state.** Audit §F calls it the best-written state in the app — it names the query, suggests a narrower action, points at the alternative tool and offers a way out. Only the terminal period changes.

**Do not touch** `"Who can reach this player"` (audit §F praises it: a system setting named by what the person controls) or the Account danger-zone copy.

- [ ] **Step 1: Write the failing check**

```bash
grep -rn "Library lens\|Local metadata layer\|Player preferences\|Plain or LRC\|Telegram contacts\|Telegram library\|One-time connection\|No matches yet\|uv run python run.py\|matching contacts" static/index.html static/app.js
```

Expected now: hits in both files. Expected after: **no output** (exit 1).

And in `tests/test_layout.py`, for a rendered string that greps cannot catch:

```python
    def test_headlines_take_no_terminal_period(self):
        page = self.page(1440, 900)
        page.route("**/api/tracks*", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"items": [], "offset": 0, "total": 0}'))
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.fill("#track-search", "qqqqq")
        page.wait_for_selector("#empty-library:not([hidden])")
        headline = page.text_content("#empty-title")
        self.assertFalse(headline.rstrip().endswith("."), f"headline carries a terminal period: {headline!r}")
        # The best-written state in the app (audit F): it must still name the query.
        self.assertIn("qqqqq", headline)
```

- [ ] **Step 2: Run the check to verify it fails**

Run the grep above.
Expected: FAIL — 10+ matching lines across the two files.

Run: `./.venv/bin/python -m unittest tests.test_layout -v`
Expected: FAIL with `headline carries a terminal period: 'Nothing matches "qqqqq".'`

- [ ] **Step 3: Delete the eight eyebrows**

In `static/index.html`, remove the `<p class="eyebrow">…</p>` element on lines 27, 44, 129, 301, 311, 339, 354, 367. Three sit inside a wrapper `<div>` that exists only to pair them with a heading (`:129`, `:301`, `:311`, `:339`, `:367`) — unwrap the heading rather than leaving an empty div. For example at `:129`:

```html
              <strong id="global-results-title">Search everywhere</strong>
              <span id="global-results-count" class="small-copy"></span>
```

(that `#global-results-count` span is Task 20's; if this task lands first, add it there and Task 20 just fills it.)

- [ ] **Step 4: Fix the strings**

`static/app.js:641-644`, in `renderTracks`' empty branch:

```js
    $("empty-title").textContent = query ? `Nothing matches "${query}"` : "Add a channel, bot, or private chat";
```

`static/app.js:803`:

```js
  empty.textContent = message || "Nothing matches that search";
```

`static/app.js:1559` — say nothing rather than a number that predates the query:

```js
  const typed = $("share-search").value.trim();
  $("share-status").textContent = typed ? `${contacts.length} ${contacts.length === 1 ? "match" : "matches"}` : "";
```

`static/app.js:1560` — replace `"Telegram contact"`:

```js
${contact.username ? `@${escapeHtml(contact.username)}` : "Your cloud storage"}
```

`static/app.js:1664` and `static/app.js:2083` — **both** cache strings gain the noun:

```js
    const cache = await api("/api/cache/status"); $("cache-usage").textContent = `${cache.files} songs cached · ${(cache.bytes / 1048576).toFixed(1)} MB`;
```
```js
$("cache-usage").textContent = "0 songs cached · 0 MB";
```

`static/app.js:1697` — the shell command goes:

```js
    notice.textContent = "This setting needs a restart to take effect.";
```

`static/app.js:1183` — an *uncounted* source must not read as an empty one. **I traced this, and `??` alone cannot fix it:** `telegram_service.py:548` builds the discover payload with `"trackCount": int(known.get(...).get("trackCount", 0))` — it defaults to **`0`, never null** — so `item.trackCount ?? "…"` always takes the `0` branch and the existing `"…"` fallback is dead code. That is the real mechanism behind audit D9.

`musicFileCount` genuinely is absent until the counting job reports (it is only assigned at `app.js:1168`, `if (current.result?.[item.chatId] != null)`), so the distinction to test is "has this been counted at all", not "is the number zero":

```js
    const counted = item.musicFileCount ?? (item.trackCount > 0 ? item.trackCount : null);
    return `… <small>${escapeHtml(sourceKindLabel(item.kind))} · ${counted === null ? "—" : `${counted.toLocaleString()} music files`}</small> …`;
```

This does conflate "counted and genuinely empty" with "never counted" — both show `—`. That is the right trade for now: a truly empty source is rare, an uncounted one is the normal state before the job finishes, and claiming `0` for a source nobody has scanned is the actual lie. `// ponytail: counted-and-empty is indistinguishable from uncounted here; needs a real "counted" flag in the discover payload to separate them.`

`static/index.html:400`:

```html
          <p class="field-help" id="bind-host-help">This machine only keeps the player private. Anyone on my network lets other devices reach it — including anyone else on the same Wi-Fi.</p>
```

- [ ] **Step 5: Re-run the checks**

Run the grep from Step 1.
Expected: no output.

Run: `./.venv/bin/python -m unittest tests.test_layout -v` and `./.venv/bin/python -m unittest discover -s tests` and `node --test static/*.test.js`
Expected: PASS, `OK`

- [ ] **Step 6: Read every changed surface**

Open each dialog whose eyebrow was removed and confirm the heading still reads as a heading rather than floating. Check the share dialog before typing (no count) and after (a count). Confirm the network help now warns about the exposing option — that is the one with a real consequence and it had no warning at all.

- [ ] **Step 7: Commit**

```bash
git add static/index.html static/app.js tests/test_layout.py
git commit -m "Copy pass: cut implementation language and unearned numbers

LIBRARY LENS and LOCAL METADATA LAYER were implementation language; nobody has a
lens. Eight of ten eyebrows are gone -- several restated the heading directly
below them -- keeping only the two that classify something.

Specific lies fixed: '5 matching contacts' rendered before anything was typed;
Saved Messages was labelled 'Telegram contact' when it is your own cloud storage;
'41 cached' had no noun (in two places); an unscanned source showed '0 music
files', identical to an empty one; and Settings printed 'Restart with: uv run
python run.py', a shell command in an end-user preference pane.

The network help explained only the safe option -- 'Anyone on my network' is the
choice with an actual exposure consequence and it carried no warning. Headlines
lose their terminal periods. The filter-no-match state keeps its wording; audit F
calls it the best-written state in the app."
```

---

## Phase 4 — the signature and mobile (tasks 23–24)

### Task 23: the label disc

**Files:**
- Modify: `static/index.html:217-220` (`.large-art-wrap`)
- Modify: `static/style.css:573-586` (the wrap and its children), `:546` and `:884` (compact sizes)
- Modify: `static/app.js:935-940` (`updateTransport` toggles `is-playing`), `:913` area (`setTrackUi` sets the stamped title)
- Test: `tests/test_layout.py`

**Interfaces:**
- Consumes: `--stamp`, `--rule`, `--paper`, `--font-display` (Tasks 13, 14)
- Produces: `.label-disc` and `.label-disc.is-playing` — **both are on Task 12's `--stamp` selector allowlist**, so use these exact class names or the token test fails

Spec §4.4, the signature. The now-playing artwork becomes a record label: circular crop, a concentric ring at the label edge in `--rule`, and a real spindle hole (a `--paper` dot with an inset shadow).

**Rotation speed is a deliberate correction to realism.** 33⅓ RPM is 1.8s per revolution, which reads as a fidget spinner. **20s per revolution** — alive, not annoying. Holds position on pause rather than resetting, so pausing looks like lifting the needle.

**Static under `prefers-reduced-motion: reduce`, no rotation ever.** The app honours that query in 14 places already (11 in `app.js`, 3 in CSS) and audit §F calls it out as genuinely rare — match the existing pattern rather than inventing one. Doing it in CSS is better here than the JS `matchMedia` used at `app.js:1381`/`:1423`, because the animation is declared in CSS and a media query cannot get out of sync with a listener.

**No artwork → an actual white label.** Spec §4.4 calls this the payoff: the product's weakest data state becomes its most characteristic image. `#large-art-placeholder` (`index.html:219`) is already the no-artwork element, so the blank disc replaces its generic placeholder rather than adding a new node.

Existing geometry helps: `.large-art-wrap` (`style.css:573-580`) already has `position: relative`, `aspect-ratio: 1` and `overflow: hidden`, and `style.css:562` already sets `will-change: transform` on it for the FLIP. So this is a `border-radius` change plus pseudo-elements, not new layout.

- [ ] **Step 1: Write the failing test**

```python
    def test_label_disc_is_circular_and_only_spins_while_playing(self):
        page = self.page(1440, 900)
        self.open_now_panel(page)
        page.evaluate("""() => {
          document.querySelector('.large-art-wrap').classList.add('label-disc');
          document.querySelector('.now-title h2').textContent = 'Rival Dealer';
        }""")
        shape = page.evaluate("""() => {
          const disc = document.querySelector('.label-disc');
          const style = getComputedStyle(disc);
          const box = disc.getBoundingClientRect();
          return {
            radius: style.borderRadius,
            square: Math.abs(box.width - box.height) < 1,
            spinning: style.animationName,
            duration: style.animationDuration,
          };
        }""")
        self.assertTrue(shape["square"], "a label must be a circle, so the box has to be square")
        self.assertEqual("50%", shape["radius"])
        self.assertEqual("none", shape["spinning"], "the disc must not rotate while paused")

        page.evaluate("() => document.querySelector('.label-disc').classList.add('is-playing')")
        playing = page.evaluate("""() => {
          const style = getComputedStyle(document.querySelector('.label-disc'));
          return { name: style.animationName, duration: style.animationDuration };
        }""")
        self.assertEqual("label-spin", playing["name"])
        # 20s, not 1.8s: 33 1/3 RPM reads as a fidget spinner at this size.
        self.assertEqual("20s", playing["duration"])

    def test_label_disc_holds_still_for_reduced_motion(self):
        page = self.browser.new_page(viewport={"width": 1440, "height": 900},
                                     reduced_motion="reduce")
        page.route("**/api/**", self._stub)
        page.goto(f"http://127.0.0.1:{self.port}/index.html", wait_until="load")
        self.addCleanup(page.close)
        self.open_now_panel(page)
        page.evaluate("""() => {
          const disc = document.querySelector('.large-art-wrap');
          disc.classList.add('label-disc', 'is-playing');
        }""")
        name = page.evaluate("() => getComputedStyle(document.querySelector('.label-disc')).animationName")
        self.assertEqual("none", name, "reduced-motion users must never get a spinning disc")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./.venv/bin/python -m unittest tests.test_layout -v`
Expected: FAIL with `AssertionError: '50%' != '6px'` — the wrap is still a rounded square.

- [ ] **Step 3: Make the disc**

`static/index.html:217-220` — the class goes on the existing wrap, and the blank label gets a slot for the stamped title:

```html
          <div class="large-art-wrap label-disc">
            <img id="large-art" class="large-art" alt="" hidden>
            <div id="large-art-placeholder" class="art-placeholder large white-label" aria-hidden="true"><span id="label-stamp" class="label-stamp"></span></div>
          </div>
```

`static/style.css:573-586`:

```css
.large-art-wrap {
  position: relative;
  margin: 20px 0 23px;
  aspect-ratio: 1;
  overflow: hidden;
  border-radius: var(--radius-control);
  box-shadow: var(--elev-2);
}
/* A dubplate: circular crop, a printed ring at the label edge, and a real spindle hole. */
.label-disc { border-radius: 50%; }
.label-disc::after {
  position: absolute;
  z-index: 3;
  top: 50%;
  left: 50%;
  width: 7%;
  height: 7%;
  border-radius: 50%;
  background: var(--paper);
  box-shadow: inset 0 1px 3px rgb(0 0 0 / .45);
  content: "";
  transform: translate(-50%, -50%);
}
.label-disc::before {
  position: absolute;
  z-index: 3;
  inset: 6%;
  border: 1px solid var(--rule);
  border-radius: 50%;
  content: "";
  pointer-events: none;
}
/* 20s per revolution. 33 1/3 RPM is 1.8s, which reads as a fidget spinner rather than a record.
   The animation stays on the element while paused so it holds position instead of snapping
   back to 0deg -- pausing should look like lifting the needle. */
.label-disc.is-playing { animation: label-spin 20s linear infinite; }
.label-disc:not(.is-playing) { animation: label-spin 20s linear infinite; animation-play-state: paused; }
@keyframes label-spin { to { transform: rotate(360deg); } }
/* The one bold element in the app is also the one that must never move for people who asked
   for stillness. Declared in CSS so it cannot drift out of sync with a JS listener. */
@media (prefers-reduced-motion: reduce) {
  .label-disc.is-playing, .label-disc:not(.is-playing) { animation: none; }
}
.large-art, .art-placeholder { width: 100%; height: 100%; border: 1px solid var(--rule); border-radius: 50%; object-fit: cover; }
.large-art-wrap > .large-art, .large-art-wrap > .art-placeholder { position: absolute; inset: 0; }
.large-art { z-index: 1; opacity: 0; transition: opacity 200ms var(--ease-out); }
.large-art.is-ready { opacity: 1; }
#large-art-placeholder { transition: opacity 200ms var(--ease-out); }
#large-art-placeholder.is-covered { opacity: 0; }
/* The payoff: no artwork is the common case here, so it becomes the characteristic image --
   a blank white label with the title stamped across it. */
.white-label { display: grid; place-items: center; padding: 22%; background: var(--paper); }
.label-stamp {
  color: var(--ink);
  font-family: var(--font-display);
  font-size: clamp(11px, 2.2vw, 20px);
  font-stretch: 62%;
  font-weight: 620;
  letter-spacing: .06em;
  line-height: 1.15;
  text-align: center;
  text-transform: uppercase;
  overflow: hidden;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
}
```

The `:not(.is-playing)` rule with `animation-play-state: paused` is what holds position; `animation: none` would reset the transform to 0deg and the disc would snap upright on pause.

`static/app.js` — in `updateTransport` (`app.js:935`), which already computes `playing`:

```js
  document.querySelector(".label-disc")?.classList.toggle("is-playing", playing);
```

And in `setTrackUi`, beside where the title is set, stamp the blank label:

```js
  $("label-stamp").textContent = metadata.title || track.file?.name || "Untitled";
```

`static/style.css:546` and `:884` — the compact sizes stay as they are (64px and 56px); a small circle still reads as a label, and Task 2's measured `--compact-header` depends on those exact heights.

- [ ] **Step 4: Run the tests**

Run: `./.venv/bin/python -m unittest tests.test_layout -v`
Expected: PASS

Run: `node --test static/*.test.js`
Expected: PASS — including Task 12's `--stamp` allowlist, which permits `.label-disc.is-playing`.

Run: `./.venv/bin/python -m unittest discover -s tests`
Expected: PASS, `OK`

- [ ] **Step 5: Watch it**

Play a track with artwork and confirm the disc turns slowly and the spindle hole stays centred. Pause and confirm it **holds its angle** rather than snapping upright. Then play a track with no artwork — the common case — and confirm a blank white label with the title stamped in condensed caps, clipped to the circle at 320px, 390px and 1440px. Long titles should clamp to three lines, not overflow the disc.

- [ ] **Step 6: Commit**

```bash
git add static/index.html static/style.css static/app.js tests/test_layout.py
git commit -m "The now-playing artwork becomes a label disc

Circular crop, a printed ring in --rule, and a real spindle hole. It turns only
while playing, at 20s per revolution -- 33 1/3 RPM is 1.8s, which reads as a
fidget spinner rather than a record -- and holds its angle on pause instead of
snapping upright, so pausing looks like lifting the needle.

No artwork is the common case in a library of other people's rips, so that state
becomes the characteristic image: a blank white label with the title stamped
across it in condensed caps, clipped to the disc. The product's weakest data state
is now its most recognisable one.

Reduced motion is honoured in CSS rather than JS, so a media query cannot drift
out of sync with a listener. This is the only bold element; everything around it
stays quiet."
```

---

### Task 24: the mobile layout pass

**Files:**
- Modify: `static/style.css:1238-1244` (the coarse-pointer block), `:912` (the 860px row grid), plus a new `max-width: 360px` block
- Modify: `static/app.js` (a row bottom sheet reusing `openMenu`'s action shape)
- Test: `tests/test_layout.py`

**Interfaces:**
- Consumes: the 7-column grid and `ordinal` from Task 18; `openMenu(actions, x, y)` from `app.js:1411`
- Produces: nothing

Spec §4.5: not an adaptation, a second designed layout. Tasks 8 and 9 already did the two structural pieces (the panel stops above the player; the filter and count come back). This is what remains: reachable row actions, and a title-priority rule at the narrowest width.

**One audit claim I tested and it does not hold.** The audit says row actions are "27px icons" on mobile, but `style.css:1240` already applies `min-width: 44px; min-height: 44px` to `:where(.button, .icon-button, .play-button, .add-source, .text-button, .tab, .row-menu, .context-menu button)` under `(hover: none), (pointer: coarse)`. I checked whether the more specific `.track-row-actions .icon-button { width: 32px; height: 32px }` at `style.css:506` defeats it — **it does not**: I measured both a `.track-row-actions .icon-button` and a `.row-menu` at 390px with touch emulation and both computed **44×44**. `min-*` constrains the used value regardless of specificity, so it wins over a more specific `width`. So do **not** "fix" the target sizes; they are already correct. What is actually wrong is that two separate 44px targets plus a duration column consume a fixed ~120px of a 294px content width (audit D7), which is a *layout* problem, not a target-size one.

**So the fix is to move the actions off the row.** Spec §4.5: rows keep ordinal, title, artist and date; duration and like move into a bottom sheet. That reclaims ~120px for titles, which is the actual complaint — at 320px titles truncate to ~11 characters ("Rival Deal…", "Sines (ta…") while subtitles truncate too ("Kode9 & Th…").

**Reuse `openMenu`, do not build a second menu.** It already takes `[{label, action, danger}]` and its dispatch at `app.js:2091` reads `menu._actions[i].action`. A sheet is that same list positioned differently, so the difference belongs in CSS.

**Remember the duplicated rules.** `.row-menu, .track-row-actions { opacity: 1 }` is declared at **both** `style.css:915` and `:1239`, and `.transport .mode-button { display: none }` at **both** `:893` and `:1233`. Task 4 removed the `opacity: 0` default that made those necessary, so both copies of the `opacity: 1` override should now be deleted rather than edited — an override of a default that no longer exists is dead weight that will confuse the next reader.

- [ ] **Step 1: Write the failing test**

```python
    def test_narrow_rows_give_their_space_to_titles(self):
        page = self.page(320, 720)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_selector(".track-row:not(.track-placeholder)")
        shape = page.evaluate("""() => {
          const row = document.querySelector('.track-row:not(.track-placeholder)');
          const hidden = (selector) => {
            const el = row.querySelector(selector);
            return !el || getComputedStyle(el).display === 'none';
          };
          return {
            rowWidth: Math.round(row.getBoundingClientRect().width),
            copyWidth: Math.round(row.querySelector('.track-copy').getBoundingClientRect().width),
            ordinalWidth: Math.round(row.querySelector('.track-ordinal').getBoundingClientRect().width),
            postedHidden: hidden('.track-posted'),
            durationHidden: hidden('.track-duration'),
            likeHidden: hidden('.track-row-actions'),
          };
        }""")
        # Duration and like move to the sheet; the date drops before any title truncation.
        self.assertTrue(shape["durationHidden"], "duration should move to the row sheet at 320px")
        self.assertTrue(shape["likeHidden"], "the like button should move to the row sheet at 320px")
        self.assertTrue(shape["postedHidden"], "the date drops before the title gives up space")
        self.assertLessEqual(shape["ordinalWidth"], 30, "the ordinal should shrink to ~3ch")
        # The title must now get most of the row rather than ~60% of it.
        self.assertGreater(shape["copyWidth"] / shape["rowWidth"], 0.7,
                           f"titles still starved: {shape}")

    def test_row_sheet_opens_with_reachable_targets(self):
        page = self.page(390, 844)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_selector(".track-row:not(.track-placeholder)")
        page.click(".track-row:not(.track-placeholder) .row-menu")
        page.wait_for_selector("#context-menu:not([hidden])")
        sizes = page.evaluate("""() => [...document.querySelectorAll('#context-menu button')]
          .map((button) => Math.round(button.getBoundingClientRect().height))""")
        self.assertTrue(sizes, "the row menu opened empty")
        self.assertTrue(all(height >= 44 for height in sizes), f"sheet targets under 44px: {sizes}")
        labels = page.evaluate("() => [...document.querySelectorAll('#context-menu button')].map((b) => b.textContent)")
        self.assertTrue(any("ike" in label for label in labels), f"like moved off the row but not into the sheet: {labels}")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./.venv/bin/python -m unittest tests.test_layout -v`
Expected: FAIL with `AssertionError: False is not true : duration should move to the row sheet at 320px`

- [ ] **Step 3: Give the row's fixed columns back to the title**

`static/style.css:912`, the 860px row grid — the ordinal shrinks and the two action columns go:

```css
  .track-row { grid-template-columns: 30px minmax(0, 1fr) 72px 44px; height: var(--row-height); padding: 7px 3px; column-gap: 8px; }
  .track-head { display: none; }
  /* Duration and like live in the row's sheet from here down; a 44px target plus a duration
     column was taking ~120px of a 294px row, which is what starved the titles. */
  .track-row .track-duration, .track-row .track-row-actions { display: none; }
```

A new `max-width: 360px` block — at the narrowest width the date yields before the title does, per spec §4.5:

```css
@media (max-width: 360px) {
  /* Titles win the space fight: the ordinal goes to 3ch and the date drops entirely before
     any title is allowed to truncate. */
  .track-row { grid-template-columns: 3ch minmax(0, 1fr) 44px; column-gap: 6px; }
  .track-row .track-posted { display: none; }
  .library-heading h1 { font-size: var(--text-title); }
}
```

`static/style.css:1238-1244` — delete the two `opacity: 1` overrides (Task 4 removed the `opacity: 0` they were countering) and the now-wrong count hide (Task 9 removed the width-based one; this is its coarse-pointer twin):

```css
@media (hover: none), (pointer: coarse) {
  :where(.button, .icon-button, .play-button, .add-source, .text-button, .tab, .row-menu, .context-menu button) { min-width: 44px; min-height: 44px; }
  .progress { top: -10px; height: 20px; }
}
```

Also delete the dead `.row-menu, .track-row-actions { opacity: 1 }` at `style.css:915`.

- [ ] **Step 4: Put duration and like in the sheet**

The row menu handler already exists (`data-track-menu`). Add the two hidden affordances to its action list when they are not visible on the row, so the sheet carries everything the row dropped:

```js
function trackMenuActions(track) {
  const narrow = matchMedia("(max-width: 860px)").matches;
  return [
    // Only when the row is not already showing them, so the desktop menu does not grow
    // duplicates of controls sitting two columns away.
    ...(narrow ? [
      { label: track.liked ? "Remove from Liked songs" : "Add to Liked songs", action: () => toggleLike(track.key) },
      { label: `Duration ${formatTime(track.durationMs / 1000)}`, action: null },
    ] : []),
    { label: "Play next", action: () => queueNext(track.key) },
    { label: "Add to queue", action: () => queueAppend(track.key) },
    { label: "Edit metadata", action: () => openMetadata(track) },
    { label: "Share", action: () => openShare(track) },
  ];
}
```

A `null` action is a label-only row; `app.js:2091` already does `action?.()`, so it is a no-op rather than an error. Check the real names of the queue/like/share helpers before wiring — reuse them, do not add new ones.

`static/style.css` — the same `#context-menu` becomes a sheet at the bottom on narrow screens, so no second component exists:

```css
@media (max-width: 860px) {
  /* Same menu, same actions, positioned as a sheet. JS keeps setting left/top, so they are
     overridden here rather than special-cased in openMenu. */
  #context-menu {
    top: auto !important;
    right: 8px;
    bottom: calc(var(--player-height) + 8px);
    left: 8px !important;
    width: auto;
    border-radius: var(--radius-panel);
  }
  #context-menu button { justify-content: flex-start; padding: 12px 14px; }
}
```

- [ ] **Step 5: Run the tests**

Run: `./.venv/bin/python -m unittest tests.test_layout -v`
Expected: PASS

Run: `./.venv/bin/python -m unittest discover -s tests` and `node --test static/*.test.js`
Expected: PASS, `OK`

- [ ] **Step 6: Drive it at three widths**

At 390px and 320px: confirm titles are no longer clipped at ~11 characters, the sheet opens above the player rather than behind it, and every sheet row is comfortably tappable. Then confirm the desktop menu at 1440px did **not** gain the duplicate like/duration rows. Compare against `docs/design/baseline/29-390-library.png` and `31-320-library-truncation.png`.

- [ ] **Step 7: Commit**

```bash
git add static/style.css static/app.js tests/test_layout.py
git commit -m "Mobile rows give their fixed columns back to the title

At 320px titles truncated to ~11 characters while duration, like and menu held a
fixed ~120px of a 294px content width. Duration and like now move into the row's
sheet, the ordinal shrinks to 3ch, and the date drops before any title is allowed
to truncate.

The sheet is the existing context menu repositioned in CSS, not a second
component, so the action list and its dispatch stay in one place.

Note the audit's '27px targets' claim does not hold: style.css:1240 already forces
44px minimums under (hover: none), (pointer: coarse), and I measured both
.track-row-actions .icon-button and .row-menu at 44x44 on a touch viewport --
min-* constrains the used value regardless of the more specific width. The real
problem was how much row those correctly-sized targets consumed. Also drops two
opacity: 1 overrides that countered a default Task 4 removed."
```

---

## Self-Review

Run after the plan is written, before execution starts.

**1. Spec coverage.** Every section of `2026-07-30-redesign-spec.md` maps to at least one task:

| Spec | Task(s) |
|---|---|
| §3.1 colour, contrast table, `--stamp` rule | 12, 13 |
| §3.2 type, three faces, 11px floor | 12, 14 |
| §3.3 geometry tokens | 13, 18 |
| §4.1 numbered dated run, sort, SOURCE column | 16, 17, 18, 19 |
| §4.2 rail: kind labels, counts, `⋯` menu, bulk verb | 4, 5 |
| §4.3 Now Playing: header collapse, queue, details | 1, 2, 21 |
| §4.4 the label disc | 23 |
| §4.5 mobile as a second layout | 8, 9, 24 |
| §4.6 unified search rows | 20 |
| §4.7 copy rules | 22 (plus 1, 4, 11) |
| §6 success criteria 1-8 | 1-3, 8-12, 18-19 |

Audit section A: A1→8, A2→1, A3→2, A4/A5→4, A6→5, A7→6, A8→7, A9→9, A10→10, A11 retracted (not resurrected). Section B: B1/B2→13, B3→13, B4→14, B5→13, B6→14, B7→15, B8→11. Section C: C1/C2/C5/C6→13, C3→14, C4→14. Section D: D1→4, D2 **not addressed** (see gaps), D3→17/19, D4→21, D5→18, D6/D8→20, D7→24, D9→22, D10→4, D11→4. Section E→22.

**Known gaps, stated rather than hidden:**
- **Audit D2** (two track counts that can disagree — the rail's summed `trackCount` vs the header's live `totalTracks`) has **no task**. It needs a decision about which is authoritative, which is a product question, not a redesign one. Flag to the user rather than silently dropping.
- Spec §4.1's **day rules in All music** (`── 29 JUL ──`) are not implemented; they interact with the 40-row virtualiser window in a way that needs its own task.
- Spec §4.3's **expanded header ≤45%** cap is noted inside Task 2 as deliberately deferred.

**2. Placeholder scan.** No `TBD`, `TODO`, "implement later", "add appropriate error handling", or "similar to Task N" survives. Every code step carries real code. Tasks 5, 6, 7 and 22 use documented manual probes instead of fake tests, and say so explicitly.

**3. Type consistency.** Names are consistent across tasks: `queueView`, `shouldCompactHeader` (Tasks 1-2, `player-core.js`); `sourceKindLabel`, `errorCopy`, `AppError`, `formatPostedDate`, `formatSyncedAt`, `ordinal` (Tasks 4, 11, 16, all in `format.js`); `trackRowHeight`, `libraryParameters`, `detailRowsFor` (`app.js`); `_TRACK_SORTS`, `list_tracks(..., sort=)` (Python). `--compact-header` is set in Task 2 and re-declared in Task 13's block. `.label-disc.is-playing` matches Task 12's allowlist exactly.

**4. Verified during planning, not assumed.** These were run, not reasoned about: the token test (executed — 3 fail, 1 passes vacuously, 21 font offenders, 8 stray hex with exact lines); all four sort orderings against a real SQLite database; the font download (3 woff2, `wOF2` magic, 119,700 bytes, browser UA required); the header geometry (132/120px compact, and that the handoff's 72 still never fires); the paused-rotation and reduced-motion behaviour in Chromium; the 44px touch targets (audit claim disproved); the `/assets` rewrite requirement; the `liked_at` bug; and the two arithmetic slips in the spec's contrast table.

## Execution Handoff

Plan complete at `docs/superpowers/plans/2026-07-30-white-label-redesign.md`. Two execution options:

1. **Subagent-driven (recommended)** — a fresh subagent per task with review between tasks. Suits this plan: 24 tasks, most independently verifiable, and Phase 1 is shippable on its own.
2. **Inline execution** — batch the tasks in one session with checkpoints, via `superpowers:executing-plans`.

Sequencing constraints either way: Task 12 before 13-15; Task 8 before the deferred assertions in 5, 6, 7, 9, 10 and 24; Tasks 1 and 2 in order (they share an import line); Task 3 is fully independent.
