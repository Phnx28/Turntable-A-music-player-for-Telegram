import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const index = fs.readFileSync(new URL("./index.html", import.meta.url), "utf8");
const app = fs.readFileSync(new URL("./app.js", import.meta.url), "utf8");

const iconIds = [
  "play-filled",
  "pause",
  "prev",
  "next",
  "volume",
  "search",
  "sync",
  "plus",
  "more",
  "close",
  "download",
  "edit",
  "menu",
  "lyrics",
  "shuffle",
  "repeat",
  "collapse",
  "heart",
  "heart-filled",
  "bookmark",
  "send",
  "locate",
  "pin",
  "settings",
  "sun",
  "moon",
  "monitor",
];

test("the generated sprite contains every Hugeicons app icon", () => {
  const symbols = [
    ...index.matchAll(/<symbol id="(i-[a-z-]+)" viewBox="0 0 24 24">/g),
  ].map((match) => match[1]);
  assert.deepEqual(
    symbols,
    iconIds.map((id) => `i-${id}`),
  );
  assert.doesNotMatch(index, /ionicons/i);
  assert.doesNotMatch(index, / key="/);
  assert.match(index, /id="i-play-filled"[^>]*>.*?fill="currentColor"/);
  assert.match(index, /id="i-heart-filled"[^>]*>.*?fill="currentColor"/);
});

test("static and dynamic icon references resolve to the generated sprite", () => {
  const defined = new Set(iconIds.map((id) => `i-${id}`));
  const references = [...index.matchAll(/href="#(i-[a-z-]+)"/g)].map(
    (match) => match[1],
  );
  for (const name of app.matchAll(/\bicon\(([^)]*)\)/g)) {
    for (const id of name[1].matchAll(/"([a-z-]+)"/g))
      references.push(`i-${id[1]}`);
  }
  assert.ok(references.length > 20);
  assert.ok(references.every((id) => defined.has(id)));
});
