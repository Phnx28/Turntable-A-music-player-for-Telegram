export function formatTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor(total / 60) % 60;
  const s = total % 60;
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

export function bufferedPercent(ranges, duration) {
  if (!ranges || !Number.isFinite(duration) || duration <= 0) return 0;
  let end = 0;
  for (let index = 0; index < ranges.length; index += 1) {
    try { end = Math.max(end, Number(ranges.end(index)) || 0); } catch {}
  }
  return Math.max(0, Math.min(100, end / duration * 100));
}

export function lyricIndex(lines, milliseconds) {
  let low = 0;
  let high = lines.length - 1;
  while (low <= high) {
    const middle = (low + high) >> 1;
    if (lines[middle].startMs <= milliseconds) low = middle + 1;
    else high = middle - 1;
  }
  return high;
}

export function adjacentIndex(length, current, direction) {
  if (!length) return -1;
  return (current + direction + length) % length;
}

export function normalizeTrackPage(payload) {
  if (!payload || !Array.isArray(payload.items)) {
    return { items: [], offset: 0, total: 0, allMusicTotal: null, dayBreaks: [] };
  }
  const seenBreaks = new Set();
  const dayBreaks = Array.isArray(payload.dayBreaks) ? payload.dayBreaks
    .filter((item) => Number.isInteger(item?.index) && item.index >= 0
      && /^\d{4}-\d{2}-\d{2}$/.test(item.dayKey || ""))
    .sort((a, b) => a.index - b.index)
    .filter((item) => !seenBreaks.has(item.index) && seenBreaks.add(item.index))
    .map(({ index, dayKey }) => ({ index, dayKey })) : [];
  return {
    items: payload.items.filter((item) => item && typeof item.key === "string"),
    offset: Math.max(0, Number(payload.offset) || 0),
    total: Math.max(0, Number(payload.total) || 0),
    allMusicTotal: Number.isFinite(Number(payload.allMusicTotal))
      ? Math.max(0, Number(payload.allMusicTotal)) : null,
    dayBreaks,
  };
}

export function trackScrollTop(index, listTop, clientHeight, rowHeight, separatorHeight = 0, dayBreaks = []) {
  const position = Math.max(0, Number(index) || 0);
  const top = Math.max(0, Number(listTop) || 0);
  const row = Math.max(1, Number(rowHeight) || 1);
  const separator = Math.max(0, Number(separatorHeight) || 0);
  const separatorsBefore = Array.isArray(dayBreaks)
    ? dayBreaks.filter(({ index: breakIndex }) => Number.isInteger(breakIndex) && breakIndex < position).length
    : 0;
  // Put the target at the top of the virtual viewport first. The renderer keeps a
  // look-ahead window, so this guarantees the row exists before scrollIntoView recentres it.
  return Math.max(0, top + position * row + separatorsBefore * separator);
}

export function virtualTrackWindow({ scrollTop, listTop, total, rowHeight, separatorHeight, dayBreaks }) {
  const count = Math.max(0, Math.floor(Number(total) || 0));
  const row = Math.max(1, Number(rowHeight) || 1);
  const separator = Math.max(0, Number(separatorHeight) || 0);
  const breaks = Array.isArray(dayBreaks)
    ? dayBreaks.filter(({ index }) => Number.isInteger(index) && index >= 0 && index < count)
    : [];
  const y = Math.max(0, (Number(scrollTop) || 0) - (Number(listTop) || 0));
  const breaksThrough = (index) => breaks.filter((item) => item.index <= index).length;
  let low = 0;
  let high = count;
  while (low < high) {
    const middle = (low + high) >> 1;
    const bottom = middle * row + breaksThrough(middle) * separator + row;
    if (bottom <= y) low = middle + 1;
    else high = middle;
  }
  const firstVisible = Math.min(count, low);
  const start = Math.max(0, Math.floor(firstVisible / 40) * 40 - 40);
  const end = Math.min(count, start + 80);
  const before = breaks.filter(({ index }) => index < start).length;
  const after = breaks.filter(({ index }) => index >= end).length;
  return {
    start,
    end,
    topHeight: start * row + before * separator,
    bottomHeight: Math.max(0, count - end) * row + after * separator,
  };
}

export function normalizePlayerState(payload) {
  // v1 stored the entire queue; v2 stores only a window around the current track. Both are
  // accepted so an existing session is not thrown away on upgrade -- a rejected snapshot loses
  // the user's position silently.
  if (!payload || (payload.version !== 1 && payload.version !== 2)) return null;
  const queue = Array.isArray(payload.queue)
    ? payload.queue.filter((key) => typeof key === "string").slice(0, 100_000)
    : [];
  return {
    version: payload.version,
    queue,
    queueIndex: Math.max(-1, Math.min(Number(payload.queueIndex) || 0, queue.length - 1)),
    currentKey: typeof payload.currentKey === "string" ? payload.currentKey : "",
    position: Math.max(0, Number(payload.position) || 0),
    source: typeof payload.source === "string" ? payload.source : "",
    liked: Boolean(payload.liked),
    temporarySource: payload.temporarySource && typeof payload.temporarySource.chatId === "string"
      ? payload.temporarySource : null,
    panel: ["lyrics", "queue", "details"].includes(payload.panel) ? payload.panel : "lyrics",
    panelOpen: Boolean(payload.panelOpen),
    // Absent on v1, where queue held everything, so it falls back to the stored length.
    queueTotal: Math.max(queue.length, Number(payload.queueTotal) || 0),
    // Absolute position of queue[0] in the full playlist; 0 for v1/full snapshots.
    queueOffset: Math.max(0, Number(payload.queueOffset) || 0),
  };
}

export function queueView(queue, queueIndex, total = null, offset = 0) {
  // A restored or server-windowed queue holds only part of the real playlist; the summary must
  // read the server's total and absolute position, or 54,660 tracks display as "351 in queue".
  const queueCount = Array.isArray(queue) ? queue.length : 0;
  const count = total != null ? Math.max(queueCount, Number(total) || 0) : queueCount;
  const relative = Math.max(-1, Number.isFinite(queueIndex) ? queueIndex : -1);
  const absolute = (Number(offset) || 0) + relative;
  // A cursor past the end is a stale index (full queue); with a window offset the absolute
  // position is real and may legitimately exceed the stored slice, so only clamp the former.
  const cursor = offset > 0 ? Math.max(-1, absolute) : Math.max(-1, Math.min(absolute, queueCount - 1));
  const upcoming = Math.max(0, count - cursor - 1);
  const summary = !count ? "Your queue is empty"
    : upcoming ? `${count.toLocaleString()} in queue · ${upcoming.toLocaleString()} up next`
    : `${count.toLocaleString()} in queue · last track`;
  return { total: count, upcoming, isEmpty: count === 0, summary };
}

// -- Queue-window transitions ---------------------------------------------------
//
// A full-library queue is 54,660 keys -- a 1.2 MB snapshot, a quarter of the localStorage
// quota, costing milliseconds of synchronous work on every play, pause and seek. Only a window
// around the current track is worth persisting or drawing: the queue pane never renders more
// than queueIndex+101 and prefetch reads a handful. Everything past the window is rebuilt from
// the server on demand, which is where the ordering came from in the first place. The four-way
// contract among the window, its total, its offset and its truncation marker lives here, in one
// module, so the call sites in app.js cannot re-derive it with subtly different rules.

export function snapshotWindow(queue, queueIndex, { total, truncated, offset, behind = 50, ahead = 300 }) {
  const start = Math.max(0, queueIndex - behind);
  const slice = queue.slice(start, queueIndex + ahead);
  return {
    queue: slice,
    queueIndex: Math.max(-1, queueIndex - start),
    // Records how many tracks were really queued, so move() knows there is more to rebuild.
    // When queue is itself a restored window, its length is NOT the real total -- taking it
    // would collapse 54,660 to 300 on the first re-save and lose the rest of the library.
    queueTotal: Math.max(queue.length, truncated ? total || 0 : 0),
    // Absolute position of the first stored key, so a restored window's "up next" counts
    // against the whole library, not against the slice that happens to be in memory.
    queueOffset: (offset || 0) + start,
  };
}

export function windowFromResult(keys, total, offset) {
  // The server returns {keys, offset, total}. The window is complete by definition, but the
  // library it came from may be larger; the truncated marker and total travel with the window
  // so move() and the queue summary know, and re-persisting does not shrink the real total.
  const realTotal = Number.isInteger(total) ? total : keys.length;
  const realOffset = Number.isInteger(offset) ? offset : 0;
  return { total: realTotal, offset: realOffset, truncated: realTotal > keys.length };
}

export function resolveWindowEdge(direction, queue, queueIndex, truncated, currentKey, window) {
  // A windowed queue holds only part of the playlist, so running past either edge is not the
  // end of it. Returns {queue, queueIndex, next} for a fresh window fetched around currentKey,
  // or null when no rebuild is needed. Ordered windows contain the current track; a fresh
  // shuffle does not, so it falls back to starting at the top of the new order.
  const next = queueIndex + direction;
  if (!((next < 0 || next >= queue.length) && truncated)) return null;
  if (!window?.keys?.length) return null;
  const resumeAt = window.keys.indexOf(currentKey);
  const resolvedIndex = resumeAt >= 0 ? resumeAt : 0;
  return { queue: window.keys, queueIndex: resolvedIndex, next: resolvedIndex + direction };
}

export function toggleShuffleQueue(keys, currentKey, enableShuffle) {
  // A shuffled window excludes the current track (the server's shuffle drops it), so it is
  // prepended; an ordered window includes it at position `windowBefore`, so the copy must be
  // dropped before re-adding -- otherwise the track queues twice. Without a shuffle, a window
  // from the top may not contain the current track at all, so it is prepended too.
  const queue = enableShuffle && currentKey
    ? [currentKey, ...keys.filter((key) => key !== currentKey)]
    : keys;
  return currentKey && !queue.includes(currentKey) ? [currentKey, ...queue] : queue;
}

export function explicitQueue(queue, key, explicitIndex) {
  // An explicit queue (a global-search result, a temporary source) replaces the window: it is
  // complete by definition, and stale truncation metadata would make move() rebuild against
  // the wrong filter.
  const queueIndex = Number.isInteger(explicitIndex) ? explicitIndex : Math.max(0, queue.indexOf(key));
  return { queue, queueIndex, queueTotal: queue.length, queueTruncated: false, queueOffset: 0 };
}

export function restoreWindow(savedQueue, savedQueueIndex, savedTotal, savedOffset) {
  // Marks that more tracks existed than were stored, so move() rebuilds instead of wrapping,
  // and keeps the real total so re-persisting does not shrink it to the window size.
  return {
    queue: savedQueue,
    queueIndex: savedQueueIndex,
    queueTruncated: savedTotal > savedQueue.length,
    queueTotal: savedTotal,
    queueOffset: Number(savedOffset) || 0,
  };
}

// The header is a flex sibling of the scroller, so collapsing it hands its freed height to
// .now-content and shrinks the maximum scrollTop by the same amount. Subtract only what the
// collapse frees (measured: 546 -> 132 desktop, 515 -> 120 phone) and require the pane to
// still be scrollable afterwards, or a short pane oscillates. Two scroll thresholds, 48 to
// collapse and 12 to expand, so the boundary is never a single point.
export function shouldCompactHeader({ scrollTop, scrollHeight, clientHeight, headerHeight, compactHeight, compact }) {
  if (compact) return scrollTop >= 12;
  if (compactHeight >= headerHeight) return false;
  const freed = Math.max(0, headerHeight - compactHeight);
  return scrollTop > 48 && (scrollHeight - clientHeight - freed) > 12;
}
