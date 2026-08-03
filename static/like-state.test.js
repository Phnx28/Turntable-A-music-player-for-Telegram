import test from "node:test";
import assert from "node:assert/strict";
import { beginLikeOperation, likedState, representationsFor, resolveLikeResponse, rollbackLikeOperation } from "./like-state.js";

const sources = (overrides = {}) => ({
  tracks: [], globalTracks: [], summaryCache: new Map(), trackCache: new Map(), current: null,
  ...overrides,
});

test("representationsFor collects every live copy of a Track once", () => {
  const shared = { key: "1:2", liked: true };
  const another = { key: "1:2", liked: false };
  const current = { key: "1:2", liked: true };
  const detail = { key: "1:2", liked: false };
  const result = representationsFor("1:2", sources({
    tracks: [shared, { key: "1:3" }],
    globalTracks: [another],
    summaryCache: new Map([["1:2", shared]]),
    trackCache: new Map([["1:2", detail]]),
    current,
  }));
  // The same object in the sparse array and the summary cache must appear once; the detail
  // cache holds its own object.
  assert.equal(result.length, 4);
  assert.deepEqual(new Set(result), new Set([shared, another, detail, current]));
});

test("likedState trusts the pending operation, then the richest representation", () => {
  const summary = { key: "1:2", liked: true };
  const detail = { key: "1:2", liked: false };
  // Pending intent wins over every stored copy.
  assert.equal(likedState("1:2", { pending: { desired: true }, current: null, trackCache: new Map(), summaryCache: new Map(), representations: [] }), true);
  // Detail beats summary.
  assert.equal(likedState("1:2", { pending: null, current: null, trackCache: new Map([["1:2", detail]]), summaryCache: new Map([["1:2", summary]]), representations: [] }), false);
  // Summary beats rows; a non-boolean is skipped; missing defaults to false.
  assert.equal(likedState("1:2", { pending: null, current: null, trackCache: new Map(), summaryCache: new Map([["1:2", summary]]), representations: [{ liked: false }] }), true);
  assert.equal(likedState("1:2", { pending: null, current: null, trackCache: new Map([["1:2", { liked: undefined }]]), summaryCache: new Map(), representations: [] }), false);
});

test("beginLikeOperation chains the baseline onto the pending operation", () => {
  const operations = new Map();
  const first = beginLikeOperation(operations, "1:2", false);
  assert.equal(first.desired, true);
  assert.equal(first.baseline, false);
  // A second flip before the first response: baseline is the first operation's baseline.
  const second = beginLikeOperation(operations, "1:2", true);
  assert.equal(second.desired, false);
  assert.equal(second.baseline, false);
  assert.notEqual(second.id, first.id);
  assert.equal(operations.get("1:2"), second, "the latest operation owns the key");
});

test("resolveLikeResponse lets only the latest operation render, but older answers update its baseline", () => {
  const operations = new Map();
  const first = beginLikeOperation(operations, "1:2", false);   // desired true
  const second = beginLikeOperation(operations, "1:2", true);   // desired false
  // First response lands late: must not render, but becomes the newer operation's rollback target.
  const stale = resolveLikeResponse(operations, "1:2", first, true);
  assert.equal(stale.applied, false);
  assert.equal(second.baseline, true);
  // The latest response renders and clears the operation.
  const fresh = resolveLikeResponse(operations, "1:2", second, false);
  assert.equal(fresh.applied, true);
  assert.equal(fresh.canonical, false);
  assert.equal(operations.has("1:2"), false);
});

test("rollbackLikeOperation restores the baseline only for the latest operation", () => {
  const operations = new Map();
  const first = beginLikeOperation(operations, "1:2", false);
  const second = beginLikeOperation(operations, "1:2", true);
  // A stale failure must not roll back over the newer intent.
  assert.equal(rollbackLikeOperation(operations, "1:2", first), null);
  assert.equal(operations.get("1:2"), second);
  assert.equal(rollbackLikeOperation(operations, "1:2", second), false);
  assert.equal(operations.has("1:2"), false);
});
