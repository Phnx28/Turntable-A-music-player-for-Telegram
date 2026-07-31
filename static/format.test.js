import test from "node:test";
import assert from "node:assert/strict";
import { AppError, errorCopy, sourceKindLabel } from "./format.js";

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
