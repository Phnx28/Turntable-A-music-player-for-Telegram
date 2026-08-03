// The Like-state operation machinery, pure and node-testable.
//
// A Track's liked state is stored in up to four places at once (the Library rows, the summary
// cache, the detail cache, and the current Track) plus the optimistic row-like operations in
// flight. The reconciliation rules -- whose answer is canonical, what an older response may
// still contribute, and what a failure rolls back to -- live here so they cannot drift between
// call sites again. The DOM wiring in app.js calls these and renders the results.

let nextOperationId = 0;

export function representationsFor(key, sources) {
  // Every live representation of a Track, deduplicated by identity: the same object can sit in
  // the sparse Library array and the summary cache at once, and mutating it once must update
  // both renderings.
  const seen = new Set();
  const add = (track) => { if (track) seen.add(track); };
  sources.tracks.filter((track) => track?.key === key).forEach(add);
  sources.globalTracks.filter((track) => track?.key === key).forEach(add);
  add(sources.summaryCache.get(key));
  add(sources.trackCache.get(key));
  if (sources.current?.key === key) add(sources.current);
  return [...seen];
}

export function likedState(key, { pending, current, trackCache, summaryCache, representations }) {
  // An in-flight operation is the current truth, whatever the server last said.
  if (pending) return pending.desired;
  const currentTrack = current?.key === key ? current : null;
  const detail = trackCache.get(key);
  const summary = summaryCache.get(key);
  // Detail and the current Track are the richest representations. A search summary may omit
  // liked entirely, so only a real boolean participates; rows and global results are the
  // final fallback.
  return [currentTrack, detail, summary, ...representations]
    .find((track) => typeof track?.liked === "boolean")?.liked ?? false;
}

export function beginLikeOperation(operations, key, previous) {
  // A new optimistic intent chains off the pending operation's baseline: if the user flips a
  // Track twice before the first response lands, the second operation's rollback target is the
  // state before the first, not the stale pre-click state.
  const pending = operations.get(key);
  const operation = {
    id: ++nextOperationId,
    baseline: pending?.baseline ?? previous,
    desired: !previous,
  };
  operations.set(key, operation);
  return operation;
}

export function resolveLikeResponse(operations, key, operation, canonical) {
  const latest = operations.get(key);
  if (latest !== operation) {
    // An older response cannot render over a newer optimistic intent, but its canonical answer
    // is still the baseline that the newer operation must roll back to if it fails.
    if (latest && typeof canonical === "boolean") latest.baseline = canonical;
    return { applied: false, canonical };
  }
  operations.delete(key);
  return { applied: true, canonical };
}

export function rollbackLikeOperation(operations, key, operation) {
  // Only the latest operation may roll back; a stale failure leaves the newer intent in place.
  if (operations.get(key) !== operation) return null;
  operations.delete(key);
  return operation.baseline;
}
