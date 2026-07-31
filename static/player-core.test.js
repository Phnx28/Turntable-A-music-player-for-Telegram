import test from "node:test";
import assert from "node:assert/strict";
import { adjacentIndex, bufferedPercent, formatTime, lyricIndex, normalizePlayerState, normalizeTrackPage, queueView } from "./player-core.js";

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
  assert.deepEqual(normalizeTrackPage(null), { items: [], offset: 0, total: 0 });
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
