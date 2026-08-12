import test from "node:test";
import assert from "node:assert/strict";
import { cursorSuffixFor, knownTotalParam, mergePageInto, pageCacheKey, recordPageCursors, shouldFetchPage } from "./library-pages.js";

test("pageCacheKey encodes every filter that changes which rows come back", () => {
  const key = pageCacheKey(100, { likedMode: false, source: "1", query: "needle", temporary: false, sort: "title" });
  assert.equal(key, "source=1&q=needle&offset=100&limit=100&liked=false&temporary=false&sort=title");
  // Liked mode replaces the source with an empty value so the server filter applies.
  const liked = pageCacheKey(0, { likedMode: true, source: "1", query: "", temporary: false, sort: "posted" });
  assert.ok(liked.startsWith("source="), "liked mode must not leak the active source");
  assert.ok(liked.includes("liked=true"));
  // A forgotten dimension is the regression this key exists to prevent -- two filters that
  // return different rows must never produce the same key.
  assert.notEqual(
    pageCacheKey(0, { likedMode: false, source: "1", query: "", temporary: false, sort: "posted" }),
    pageCacheKey(0, { likedMode: false, source: "1", query: "", temporary: false, sort: "title" }),
  );
  assert.notEqual(
    pageCacheKey(0, { likedMode: false, source: "1", query: "a", temporary: false, sort: "posted" }),
    pageCacheKey(0, { likedMode: false, source: "1", query: "b", temporary: false, sort: "posted" }),
  );
});

test("knownTotalParam replays the total only for pages past the first", () => {
  assert.equal(knownTotalParam(0, 54660), "");
  assert.equal(knownTotalParam(200, 54660), "&total=54660");
  assert.equal(knownTotalParam(200, 0), "");
});

test("shouldFetchPage never fetches a page that is loaded or in flight", () => {
  assert.equal(shouldFetchPage(200, new Set([200]), new Set()), false);
  assert.equal(shouldFetchPage(200, new Set(), new Set([200])), false);
  assert.equal(shouldFetchPage(200, new Set(), new Set()), true);
});

test("mergePageInto slots pages into the sparse Library array", () => {
  const target = [];
  const merged = mergePageInto(target, {
    offset: 200, total: 500,
    items: [{ key: "a" }, { key: "b" }],
  }, () => {});
  assert.equal(merged, 2);
  assert.equal(target.length, 500, "the array resizes to the server total");
  assert.equal(target[200].key, "a");
  assert.equal(target[201].key, "b");
  assert.equal(target[0], undefined, "unloaded offsets stay sparse");
  // The callback feeds the summary cache per merged Track.
  const seen = [];
  mergePageInto(target, { offset: 0, total: 500, items: [{ key: "c" }] }, (track) => seen.push(track.key));
  assert.deepEqual(seen, ["c"]);
  assert.equal(target[0].key, "c");
});

test("cursorSuffixFor only applies to the posted no-filter path", () => {
  const cursors = new Map([[100, { cursor: "5:20" }]]);
  assert.equal(cursorSuffixFor(100, { likedMode: false, source: "", query: "", sort: "posted" }, cursors),
    "&cursor=5%3A20");
  assert.equal(cursorSuffixFor(100, { likedMode: false, source: "", query: "", sort: "posted" }, new Map()), "");
  assert.equal(cursorSuffixFor(100, { likedMode: true, source: "", query: "", sort: "posted" }, cursors), "");
  assert.equal(cursorSuffixFor(100, { likedMode: false, source: "1", query: "", sort: "posted" }, cursors), "");
  assert.equal(cursorSuffixFor(100, { likedMode: false, source: "", query: "needle", sort: "posted" }, cursors), "");
  assert.equal(cursorSuffixFor(100, { likedMode: false, source: "", query: "", sort: "title" }, cursors), "");
});

test("recordPageCursors chains forward and backward tokens", () => {
  const cursors = new Map();
  recordPageCursors(cursors, 100, { nextCursor: "9:30", prevCursor: "1:2" });
  assert.deepEqual(cursors.get(200), { cursor: "9:30" });
  assert.deepEqual(cursors.get(0), { cursor: "1:2", before: true });
  recordPageCursors(cursors, 100, {});
  assert.deepEqual(cursors.get(200), { cursor: "9:30" }, "absent tokens must not overwrite the chain");
});
