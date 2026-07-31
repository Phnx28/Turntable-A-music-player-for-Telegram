// Every human-facing string that is derived from stored data is formatted here, so the rail,
// the library header, global search and the discover dialog cannot disagree about what a
// "saved" chat is called. Pure and DOM-free, so it is testable under node --test.
const KIND_LABELS = { channel: "Channel", private: "Private chat", saved: "Saved Messages", bot: "Bot" };

export function sourceKindLabel(kind) {
  return KIND_LABELS[kind] || "Chat";
}
