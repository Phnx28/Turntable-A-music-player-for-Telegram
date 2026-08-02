import test from "node:test";
import assert from "node:assert/strict";
import { adjacentIndex, bufferedPercent, formatTime, lyricIndex, normalizePlayerState, normalizeTrackPage, queueView, shouldCompactHeader, virtualTrackWindow } from "./player-core.js";

test("player helpers", () => {
  assert.equal(formatTime(65.9), "1:05");
  assert.equal(formatTime(3825), "1:03:45");
  assert.equal(adjacentIndex(3, 2, 1), 0);
  assert.equal(adjacentIndex(3, 0, -1), 2);
  assert.equal(bufferedPercent({ length: 2, end: (index) => [15, 65][index] }, 100), 65);
  assert.equal(bufferedPercent({ length: 1, end: () => 120 }, 100), 100);
  assert.equal(bufferedPercent(null, 100), 0);
  assert.equal(bufferedPercent({ length: 1, end: () => 5 }, 0), 0);
  const lines = [{ startMs: 1000 }, { startMs: 2000 }, { startMs: 4000 }];
  assert.equal(lyricIndex(lines, 500), -1);
  assert.equal(lyricIndex(lines, 2500), 1);
  assert.deepEqual(normalizeTrackPage(null), { items: [], offset: 0, total: 0, allMusicTotal: null, dayBreaks: [] });
  assert.deepEqual(normalizeTrackPage({ items: [null, { key: "1:2" }], total: 1 }).items, [{ key: "1:2" }]);
  assert.equal(normalizePlayerState({ version: 3 }), null);
  assert.equal(normalizePlayerState(null), null);
  assert.deepEqual(normalizePlayerState({ version: 1, queue: [null, "1:2"], queueIndex: 9 }).queue, ["1:2"]);
});

test("saved queues survive the v1 to v2 windowing change", () => {
  // v1 snapshots are still in browsers; rejecting them would silently drop the saved position.
  const v1 = normalizePlayerState({ version: 1, queue: ["1:2", "1:3"], queueIndex: 1, currentKey: "1:3" });
  assert.equal(v1.version, 1);
  assert.equal(v1.currentKey, "1:3");
  // v1 has no queueTotal, so it falls back to the length it did store.
  assert.equal(v1.queueTotal, 2);

  // v2 keeps a window, and reports the real pre-trim total.
  const v2 = normalizePlayerState({ version: 2, queue: ["1:2", "1:3"], queueIndex: 0, queueTotal: 54660 });
  assert.equal(v2.version, 2);
  assert.equal(v2.queueTotal, 54660);

  // A total smaller than the stored window is nonsense; the window wins.
  assert.equal(normalizePlayerState({ version: 2, queue: ["1:2", "1:3"], queueTotal: 1 }).queueTotal, 2);
  // queueIndex is always clamped into the stored window.
  assert.equal(normalizePlayerState({ version: 2, queue: ["1:2"], queueIndex: 500 }).queueIndex, 0);
});

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

test("track pages normalize additive totals and valid ordered day breaks", () => {
  const page = normalizeTrackPage({
    items: [{ key: "1:1" }], offset: 0, total: 1, allMusicTotal: 17,
    dayBreaks: [{ index: 0, dayKey: "2025-07-30" }, { index: -1, dayKey: "bad" }],
  });
  assert.equal(page.total, 1);
  assert.equal(page.allMusicTotal, 17);
  assert.deepEqual(page.dayBreaks, [{ index: 0, dayKey: "2025-07-30" }]);
  assert.equal(normalizeTrackPage(null).allMusicTotal, null, "pre-response count stays neutral");
});

test("day separators preserve 40-track alignment and exact spacer height", () => {
  const dayBreaks = [
    { index: 0, dayKey: "2025-07-30" },
    { index: 40, dayKey: "2025-07-29" },
    { index: 80, dayKey: "2025-07-28" },
  ];
  const first = virtualTrackWindow({ scrollTop: 0, listTop: 0, total: 121, rowHeight: 52, separatorHeight: 28, dayBreaks });
  assert.deepEqual(first, { start: 0, end: 80, topHeight: 0, bottomHeight: 2160 });
  const middle = virtualTrackWindow({ scrollTop: 4300, listTop: 0, total: 121, rowHeight: 52, separatorHeight: 28, dayBreaks });
  assert.equal(middle.start % 40, 0);
  assert.ok(middle.end - middle.start <= 80);
  assert.equal(middle.topHeight + middle.bottomHeight + (middle.end - middle.start) * 52
    + dayBreaks.filter(({ index }) => index >= middle.start && index < middle.end).length * 28,
  121 * 52 + dayBreaks.length * 28, "spacers plus rendered rows have no gap or overlap");
});
