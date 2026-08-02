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
  };
}

export function queueView(queue, queueIndex) {
  const total = Array.isArray(queue) ? queue.length : 0;
  const cursor = Math.max(-1, Math.min(Number.isFinite(queueIndex) ? queueIndex : -1, total - 1));
  const upcoming = Math.max(0, total - cursor - 1);
  const summary = !total ? "Your queue is empty"
    : upcoming ? `${total.toLocaleString()} in queue · ${upcoming.toLocaleString()} up next`
    : `${total.toLocaleString()} in queue · last track`;
  return { total, upcoming, isEmpty: total === 0, summary };
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
