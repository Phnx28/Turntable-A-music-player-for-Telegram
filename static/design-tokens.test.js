import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const RAW = readFileSync(new URL("./style.css", import.meta.url), "utf8");
const HTML = readFileSync(new URL("./index.html", import.meta.url), "utf8");
const APP = readFileSync(new URL("./app.js", import.meta.url), "utf8");
// Blank comments in place: deleting them shifts line numbers, so failures would cite the wrong
// rule. Same length, same newlines, no comment content.
const CSS = RAW.replace(/\/\*[\s\S]*?\*\//g, (match) => match.replace(/[^\n]/g, " "));
const LINES = CSS.split("\n");

function tokenBlock(startPattern) {
  const start = LINES.findIndex((line) => startPattern.test(line));
  assert.notEqual(start, -1, `token block ${startPattern} not found`);
  const tokens = {};
  for (let index = start; index < LINES.length; index += 1) {
    const match = LINES[index].match(/(--[\w-]+):\s*([^;]+);/);
    if (match) tokens[match[1]] = match[2].trim();
    if (index > start && /^\}/.test(LINES[index])) break;
  }
  return tokens;
}

const luminance = (hex) => {
  const value = hex.replace("#", "");
  const full = value.length === 3 ? [...value].map((c) => c + c).join("") : value;
  const [r, g, b] = [0, 2, 4].map((offset) => parseInt(full.slice(offset, offset + 2), 16) / 255)
    .map((channel) => (channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};

const contrast = (a, b) => {
  const [high, low] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (high + 0.05) / (low + 0.05);
};

test("progressive material tokens define the approved scale", () => {
  const light = tokenBlock(/^:root\s*\{/);
  const dark = tokenBlock(/^html\[data-theme="dark"\]\s*\{/);

  assert.equal(light["--blur-soft"], "20px");
  assert.equal(light["--blur-strong"], "36px");
  assert.equal(light["--space-1"], "4px");
  assert.equal(light["--space-2"], "8px");
  assert.equal(light["--space-3"], "12px");
  assert.equal(light["--space-4"], "16px");
  assert.equal(light["--space-5"], "24px");
  assert.equal(light["--space-6"], "32px");
  assert.equal(light["--space-7"], "48px");

  for (const name of ["--glass-subtle", "--glass-medium", "--glass-strong", "--glass-border", "--glass-highlight"]) {
    assert.match(light[name], /color-mix/);
    assert.match(dark[name], /color-mix/);
    assert.notEqual(light[name], dark[name], `${name} needs a dark-theme calibration`);
  }
});

test("token contrast meets its requirement, computed not transcribed", () => {
  const light = tokenBlock(/^:root\s*\{/);
  const dark = tokenBlock(/^html\[data-theme="dark"\]\s*\{/);

  // Requirements from spec 3.1. The spec's own printed ratios contain two arithmetic slips, so
  // this asserts the threshold, never the quoted figure.
  const required = [
    [light, "--graphite", "--paper", 4.5, "secondary text"],
    [light, "--stamp", "--paper", 4.5, "the playing marker"],
    [light, "--ink", "--rule", 3.0, "progress elapsed vs remaining"],
    [light, "--graphite", "--rule", 3.0, "buffered vs remaining"],
    [light, "--ink", "--paper", 4.5, "body text"],
    [light, "--danger", "--paper", 4.5, "destructive text"],
    [light, "--ok", "--surface", 4.5, "the cache-ready state"],
    // The rail is a text background too, so it carries the same burden as --paper.
    [light, "--graphite", "--rail", 4.5, "rail secondary text"],
    [light, "--ink", "--rail", 4.5, "rail source titles"],
    [dark, "--graphite", "--paper", 4.5, "dark secondary text"],
    [dark, "--stamp", "--paper", 4.5, "dark playing marker"],
    [dark, "--ink", "--paper", 4.5, "dark body text"],
    [dark, "--danger", "--paper", 4.5, "dark destructive text"],
    [dark, "--ok", "--surface", 4.5, "dark cache-ready state"],
    [dark, "--graphite", "--rail", 4.5, "dark rail secondary text"],
    [dark, "--ink", "--rail", 4.5, "dark rail source titles"],
  ];
  for (const [block, front, back, minimum, what] of required) {
    const ratio = contrast(block[front], block[back]);
    assert.ok(ratio >= minimum,
      `${front} on ${back} (${what}) is ${ratio.toFixed(2)}:1, needs ${minimum}:1`);
  }

  // Elapsed must be separable from buffered, or the progress bar reads as one flat fill.
  const separation = contrast(light["--ink"], light["--graphite"]);
  assert.ok(separation >= 2.0, `elapsed vs buffered is only ${separation.toFixed(2)}:1`);

  // The focus ring is the app's primary keyboard affordance; it was 1.38:1 via color-mix.
  assert.match(light["--focus-ring"], /var\(--ink\)/,
    "the focus ring must land on --ink, not a translucent accent");
});

test("nothing ships below the 11px floor", () => {
  const offenders = [];
  LINES.forEach((line, index) => {
    const declaration = line.match(/font-size:\s*([^;}]+)/);
    if (!declaration) return;
    // Every px number in the value, so clamp() minima are covered too.
    for (const size of declaration[1].matchAll(/([\d.]+)px/g)) {
      if (Number(size[1]) < 11) offenders.push(`${index + 1}: ${size[1]}px`);
    }
  });
  assert.deepEqual(offenders, [], `font sizes below the 11px floor:\n${offenders.join("\n")}`);
});

test("--stamp marks only the currently playing track", () => {
  const allowed = [
    ".track-row.current", ".queue-row.current", ".progress::-webkit-slider-thumb",
    ".progress::-moz-range-thumb", ".label-disc.is-playing", ".playing-mark",
  ];
  const violations = [];
  // Rule bodies, so a selector list is checked as a whole.
  for (const rule of CSS.matchAll(/([^{}]+)\{([^}]*)\}/g)) {
    const [, selector, body] = rule;
    if (!body.includes("var(--stamp)")) continue;
    const trimmed = selector.trim();
    if (trimmed.startsWith(":root") || trimmed.startsWith("html[")) continue;
    if (!allowed.some((permitted) => trimmed.includes(permitted))) violations.push(trimmed);
  }
  assert.deepEqual(violations, [],
    `--stamp has exactly one job. Found it in:\n${violations.join("\n")}`);
});

test("Turntable owns one mixed typographic system", () => {
  assert.doesNotMatch(HTML, /data-font|data-setting="font"|data-value="(?:sans|serif|mono)"/);
  assert.doesNotMatch(APP, /data-font|dataset\.font|tm-font|\[\["theme",\s*"font"\]/);
  assert.doesNotMatch(CSS, /--font-serif|html\[data-font=/);
  assert.match(CSS, /--font-data:\s*"JetBrains Mono"/);
  assert.match(CSS, /--font-display:\s*"Be Vietnam Pro",\s*var\(--font-ui\)/);
});

test("the selected font families are self-hosted", () => {
  assert.match(CSS, /font-family:\s*"Be Vietnam Pro"/);
  assert.match(CSS, /font-family:\s*"JetBrains Mono"/);
  assert.match(CSS, /fonts\/be-vietnam-pro-400\.ttf/);
  assert.match(CSS, /fonts\/jetbrains-mono-400\.ttf/);
});

test("mixed typography assigns human-facing and data-facing roles", () => {
  assert.match(CSS, /\.brand-line, \.source-copy strong, \.track-copy strong[\s\S]*?font-family:\s*var\(--font-ui\)/);
  assert.match(CSS, /\.utility,[\s\S]*?\.source-copy small,[\s\S]*?\.source-count[\s\S]*?font-family:\s*var\(--font-data\)/);
  assert.match(CSS, /\.tab, \.button, \.text-button[\s\S]*?font-family:\s*var\(--font-ui\)/);
});

test("no hex literal outside the token blocks", () => {
  // The token blocks end before the first global component rule.
  const firstComponentLine = LINES.findIndex((line) => /^\* \{ box-sizing: border-box; \}/.test(line));
  assert.notEqual(firstComponentLine, -1, "could not locate the end of the token blocks");
  const strays = [];
  LINES.slice(firstComponentLine).forEach((line, offset) => {
    for (const hex of line.match(/#[0-9a-fA-F]{3,8}\b/g) || []) {
      strays.push(`${firstComponentLine + offset + 1}: ${hex}`);
    }
  });
  assert.deepEqual(strays, [], `hex literals belong in the token blocks:\n${strays.join("\n")}`);
});
