// Every human-facing string that is derived from stored data is formatted here, so the rail,
// the library header, global search and the discover dialog cannot disagree about what a
// "saved" chat is called. Pure and DOM-free, so it is testable under node --test.
const KIND_LABELS = { channel: "Channel", private: "Private chat", saved: "Saved Messages", bot: "Bot" };

export function sourceKindLabel(kind) {
  return KIND_LABELS[kind] || "Chat";
}

// Thrown only by api(), from a server-supplied body.error.message. Lives here rather than in
// app.js so errorCopy can be tested under node without importing a module that touches document.
export class AppError extends Error {
  constructor(message, retryable = false, code = "request_failed") {
    super(message); this.retryable = retryable; this.code = code;
  }
}

const GENERIC_FAILURE = "Something went wrong at our end. Try again in a moment.";

// error.message is only shown when the server authored it for a person. Anything else is an
// internal string: the audit found "The element has no supported sources." -- the raw
// HTMLMediaElement message -- and client-side TypeErrors in the error dialog.
export function errorCopy(error) {
  return error instanceof AppError && error.message ? error.message : GENERIC_FAILURE;
}

const DAY = 86400;
const WEEK = 7 * DAY;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function formatDayRule(dayKey) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(dayKey || "")) return "";
  const [year, month, day] = dayKey.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return "";
  return `── ${day} ${MONTHS[month - 1].toUpperCase()} ──`;
}

// nowSeconds is a parameter, not Date.now(), so every boundary here is testable. UTC getters
// throughout: mixing local getters with a UTC caller makes the year boundary machine-dependent.
export function formatPostedDate(seconds, nowSeconds) {
  const posted = Number(seconds) || 0;
  if (posted <= 0) return "—";
  const now = Number(nowSeconds) || 0;
  const elapsed = now - posted;
  // Server and browser clocks disagree; a negative age must not render as "-3m ago".
  if (elapsed < 0) return "Just now";
  if (elapsed < 3600) { const minutes = Math.floor(elapsed / 60); return minutes < 1 ? "Just now" : `${minutes}m ago`; }
  if (elapsed < DAY) return `${Math.floor(elapsed / 3600)}h ago`;
  if (elapsed < WEEK) { const days = Math.floor(elapsed / DAY); return days === 1 ? "Yesterday" : `${days}d ago`; }
  const date = new Date(posted * 1000);
  const stem = `${date.getUTCDate()} ${MONTHS[date.getUTCMonth()]}`;
  return date.getUTCFullYear() === new Date(now * 1000).getUTCFullYear()
    ? stem : `${stem} ${String(date.getUTCFullYear()).slice(2)}`;
}

// Zero-padded to the width of the total so the mono column stays aligned: 007 of 412.
export function ordinal(position, total) {
  const width = String(Math.max(1, Number(total) || 1)).length;
  return String(Math.max(0, Number(position) || 0)).padStart(width, "0");
}

export function formatSyncedAt(seconds, nowSeconds) {
  const synced = Number(seconds) || 0;
  if (synced <= 0) return "";
  const elapsed = (Number(nowSeconds) || 0) - synced;
  if (elapsed < 60) return "synced just now";
  if (elapsed < 3600) return `synced ${Math.floor(elapsed / 60)}m ago`;
  if (elapsed < DAY) return `synced ${Math.floor(elapsed / 3600)}h ago`;
  return `synced ${formatPostedDate(synced, nowSeconds)}`;
}
