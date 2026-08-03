// The Library view page machinery, pure and node-testable.
//
// loadPage/loadLibrary maintain a sparse array of Tracks plus dedup sets and a string cache
// key. Two invariants have burned the codebase before: the cache key must encode every filter
// that changes which rows come back (a forgotten one serves the previous sort's page), and a
// page that is loaded or already in flight must never be fetched twice. Both live here, so
// adding a filter means changing the key builder in one place, and the dedup rule cannot drift.

export function pageCacheKey(offset, { likedMode, source, query, temporary, sort }) {
  // This string is also the state.libraryCache key. Anything that changes which rows come back
  // must appear here, or a sort change is served the previous sort's cached page.
  return `source=${encodeURIComponent(likedMode ? "" : source)}&q=${encodeURIComponent(query)}&offset=${offset}&limit=100&liked=${likedMode}&temporary=${temporary}&sort=${encodeURIComponent(sort)}`;
}

export function knownTotalParam(offset, total) {
  // The total only changes when the filter changes, and the cache key encodes the filter, so
  // replaying it lets the server skip a COUNT(*) over the whole library on every page.
  return offset > 0 && total > 0 ? `&total=${total}` : "";
}

export function shouldFetchPage(offset, loadedPages, pageRequests) {
  // Loaded or already in flight: never fetch twice. The in-flight set is what makes rapid
  // scrolling collapse N overlapping requests into the first one.
  return !loadedPages.has(offset) && !pageRequests.has(offset);
}

export function mergePageInto(target, page, onTrack = () => {}) {
  // The virtualized list holds the whole Library as a sparse array sized to the server total;
  // pages slot into their offsets and the empty gaps are placeholders. Resizing to the total
  // keeps renderTracks' windowing math honest after a filter change.
  target.length = page.total;
  let merged = 0;
  page.items.forEach((track, index) => {
    target[page.offset + index] = track;
    onTrack(track);
    merged += 1;
  });
  return merged;
}
