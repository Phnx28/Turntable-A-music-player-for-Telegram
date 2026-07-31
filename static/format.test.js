import test from "node:test";
import assert from "node:assert/strict";
import { sourceKindLabel } from "./format.js";

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
