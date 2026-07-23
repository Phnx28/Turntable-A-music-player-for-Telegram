import test from "node:test";
import assert from "node:assert/strict";
import { adjacentIndex, bufferedPercent, formatTime, lyricIndex, normalizePlayerState, normalizeTrackPage } from "./player-core.js";

test("player helpers", () => {
  assert.equal(formatTime(65.9), "1:05");
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
  assert.equal(normalizePlayerState({ version: 2 }), null);
  assert.deepEqual(normalizePlayerState({ version: 1, queue: [null, "1:2"], queueIndex: 9 }).queue, ["1:2"]);
});
