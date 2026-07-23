export function formatTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const whole = Math.floor(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
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
    return { items: [], offset: 0, total: 0 };
  }
  return {
    items: payload.items.filter((item) => item && typeof item.key === "string"),
    offset: Math.max(0, Number(payload.offset) || 0),
    total: Math.max(0, Number(payload.total) || 0),
  };
}

export function normalizePlayerState(payload) {
  if (!payload || payload.version !== 1) return null;
  const queue = Array.isArray(payload.queue)
    ? payload.queue.filter((key) => typeof key === "string").slice(0, 100_000)
    : [];
  return {
    version: 1,
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
  };
}
