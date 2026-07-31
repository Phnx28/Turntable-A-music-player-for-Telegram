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
