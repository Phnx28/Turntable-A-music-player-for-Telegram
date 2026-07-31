import test from "node:test";
import assert from "node:assert/strict";
import { AppError, errorCopy, formatPostedDate, formatSyncedAt, ordinal, sourceKindLabel } from "./format.js";

test("source kinds render as human labels, never raw enums", () => {
  // The four kinds telegram_service.classify_entity can produce (telegram_service.py:602).
  assert.equal(sourceKindLabel("channel"), "Channel");
  assert.equal(sourceKindLabel("private"), "Private chat");
  assert.equal(sourceKindLabel("saved"), "Saved Messages");
  assert.equal(sourceKindLabel("bot"), "Bot");

  // A kind we have never seen must still not put a database value on screen. Telegram adds
  // entity types; the rail should degrade to a generic noun rather than print "megagroup".
  assert.equal(sourceKindLabel("megagroup"), "Chat");
  assert.equal(sourceKindLabel(""), "Chat");
  assert.equal(sourceKindLabel(undefined), "Chat");
  assert.equal(sourceKindLabel(null), "Chat");
});

test("only server-authored errors put their own message on screen", () => {
  // AppError is thrown solely by api() from a server-supplied body.error.message, so it is the
  // one kind of failure whose text was written for a person to read.
  assert.equal(errorCopy(new AppError("That channel is private.")), "That channel is private.");
  assert.equal(errorCopy(new AppError("Rate limited", true, "rate_limited")), "Rate limited");

  // Everything else is an internal string. "The element has no supported sources." is the real
  // HTMLMediaElement message the audit found in the dialog.
  const fallback = "Something went wrong at our end. Try again in a moment.";
  assert.equal(errorCopy(new Error("The element has no supported sources.")), fallback);
  assert.equal(errorCopy(new TypeError("candidates.map is not a function")), fallback);
  assert.equal(errorCopy("a bare string throw"), fallback);
  assert.equal(errorCopy(null), fallback);
  assert.equal(errorCopy(undefined), fallback);

  // An AppError with no usable message must not render an empty dialog.
  assert.equal(errorCopy(new AppError("")), fallback);
});

test("posted dates are relative under a week and absolute beyond it", () => {
  // Fixed UTC instant so every boundary is deterministic: 30 Jul 2026, 12:00 UTC.
  const NOW = Date.UTC(2026, 6, 30, 12, 0, 0) / 1000;
  const DAY = 86400;

  // Never state a number you do not have (spec 4.7).
  assert.equal(formatPostedDate(0, NOW), "—");
  assert.equal(formatPostedDate(undefined, NOW), "—");

  assert.equal(formatPostedDate(NOW - 30, NOW), "Just now");
  assert.equal(formatPostedDate(NOW - 45 * 60, NOW), "45m ago");
  assert.equal(formatPostedDate(NOW - 2 * 3600, NOW), "2h ago");
  assert.equal(formatPostedDate(NOW - DAY, NOW), "Yesterday");
  assert.equal(formatPostedDate(NOW - 3 * DAY, NOW), "3d ago");

  // Both sides of the seven-day cutoff: relative just under, absolute exactly on it.
  assert.equal(formatPostedDate(NOW - (7 * DAY - 3600), NOW), "6d ago");
  assert.equal(formatPostedDate(NOW - 7 * DAY, NOW), "23 Jul");
  assert.equal(formatPostedDate(NOW - 8 * DAY, NOW), "22 Jul");

  // Across years the two-digit year appears, or "25 Jun" is ambiguous in a channel archive.
  assert.equal(formatPostedDate(NOW - 400 * DAY, NOW), "25 Jun 25");

  // A clock skew between server and browser must not print "-3m ago".
  assert.equal(formatPostedDate(NOW + 500, NOW), "Just now");
});

test("ordinals pad to the width of the total", () => {
  // The ordinal is the real play position, so it has to line up in a mono column.
  assert.equal(ordinal(7, 412), "007");
  assert.equal(ordinal(7, 9), "7");
  assert.equal(ordinal(1, 1000), "0001");
  assert.equal(ordinal(412, 412), "412");
  assert.equal(ordinal(0, 0), "0");
});

test("sync timestamps stop printing seconds", () => {
  const NOW = Date.UTC(2026, 6, 30, 12, 0, 0) / 1000;
  const DAY = 86400;
  assert.equal(formatSyncedAt(0, NOW), "");
  assert.equal(formatSyncedAt(NOW - 30, NOW), "synced just now");
  assert.equal(formatSyncedAt(NOW - 300, NOW), "synced 5m ago");
  assert.equal(formatSyncedAt(NOW - 2 * 3600, NOW), "synced 2h ago");
  assert.equal(formatSyncedAt(NOW - DAY, NOW), "synced Yesterday");
  assert.equal(formatSyncedAt(NOW - 9 * DAY, NOW), "synced 21 Jul");
  assert.equal(formatSyncedAt(NOW - 400 * DAY, NOW), "synced 25 Jun 25");
});
