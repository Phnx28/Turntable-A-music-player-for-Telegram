"""Rebuild the inlined icon sprite in static/index.html from the Ionicons package.

The icons are inlined as <symbol> elements rather than loaded through Ionicons' own web
component: that component is an ESM bundle fetched from a CDN, which the app's CSP
(script-src 'self') blocks outright, and it would put a network dependency in front of the
first paint of a local-first player. Inlining keeps one request, keeps `currentColor`
working, and keeps the app usable offline.

Usage (Ionicons is a build-time source only, not a runtime dependency):

    npm install ionicons
    python3 tools/build_icons.py

Pass --check to verify the sprite in index.html matches the package without writing.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "static" / "index.html"
SVG_DIR = ROOT / "node_modules" / "ionicons" / "dist" / "svg"

# App icon id -> Ionicons file. Outline weight throughout, except the two glyphs that carry
# state or shape meaning: #i-heart-filled is the "liked" counterpart to the outline heart, and
# #i-play-filled fills the main transport button so the primary action reads as primary.
# Note: several icons referenced only through app.js's icon() helper ("pin", "repeat", "sync",
# "more", "close") look unused to a naive grep of index.html. Check both before pruning.
ICONS: dict[str, str] = {
    "play-filled": "play",
    "pause": "pause-outline",
    # skip-back/forward, not back/forward: the plain pair is the rewind/fast-forward double
    # chevron, which promises scrubbing. These buttons jump to the previous/next track.
    "prev": "play-skip-back-outline",
    "next": "play-skip-forward-outline",
    "volume": "volume-high-outline",
    "search": "search-outline",
    "sync": "sync-outline",
    "plus": "add-outline",
    "more": "ellipsis-horizontal-outline",
    "close": "close-outline",
    "download": "download-outline",
    "edit": "create-outline",
    "menu": "menu-outline",
    "lyrics": "musical-notes-outline",
    "shuffle": "shuffle-outline",
    "repeat": "repeat-outline",
    "collapse": "chevron-back-outline",
    "heart": "heart-outline",
    "heart-filled": "heart",
    # Saved Messages is a bookmark: the track goes onto your own shelf, nothing is sent to
    # anyone. "send" (the paper plane) belongs on the contact-forwarding button, which does.
    "bookmark": "bookmark-outline",
    "send": "send-outline",
    "locate": "locate-outline",
    "pin": "pin-outline",
}

START = "<!-- icons:start -->"
END = "<!-- icons:end -->"


def symbol(icon_id: str, source: str) -> str:
    """Convert one Ionicons file into a <symbol> for the sprite."""
    markup = (SVG_DIR / f"{source}.svg").read_text()
    view_box = re.search(r'viewBox="([^"]+)"', markup)
    if not view_box:
        raise SystemExit(f"{source}.svg has no viewBox")
    # Keep only the drawing; the wrapper's xmlns and class are meaningless inside a sprite.
    body = re.sub(r"^<svg[^>]*>|</svg>\s*$", "", markup.strip())
    # Ionicons hardcodes stroke="currentColor" but leaves fills literal, and its stroke widths
    # are absolute ("32px") which do not scale with a resized symbol. Normalise both so one CSS
    # rule can size and colour every icon.
    body = body.replace('stroke-width="32px"', 'stroke-width="32"')
    body = re.sub(r'fill="(?!none)[^"]*"', 'fill="currentColor"', body)
    body = re.sub(r"\s+", " ", body).strip()
    return f'<symbol id="i-{icon_id}" viewBox="{view_box.group(1)}">{body}</symbol>'


def build() -> str:
    if not SVG_DIR.is_dir():
        raise SystemExit(f"{SVG_DIR} missing -- run `npm install ionicons` first")
    return "".join(symbol(icon_id, source) for icon_id, source in ICONS.items())


def audit_references() -> tuple[list[str], list[str]]:
    """Compare what the app asks for against what the sprite defines, both directions.

    Missing ids matter because they fail *silently*: <use> renders nothing and the button stays
    clickable but empty. Dynamic references are the trap -- app.js builds some hrefs with
    `icon(cond ? "pause" : "play-filled")`, so grepping for a literal `#i-play` finds nothing
    while 80 rows render blank at runtime. Collect literal ids and icon() arguments alike.

    Unused ids are reported too, and only warned about, not failed: they cost bytes in every
    page load and quietly accumulate whenever a button is rewired. Re-pointing one button at a
    different glyph is exactly how #i-share was orphaned.
    """
    sources = [(ROOT / "static" / name).read_text() for name in ("index.html", "app.js")]
    referenced: set[str] = set()
    for text in sources:
        referenced.update(re.findall(r'href="#i-([a-z0-9-]+)"', text))
        # icon("name") and both arms of icon(cond ? "a" : "b")
        for call in re.findall(r"\bicon\(([^)]*)\)", text):
            referenced.update(re.findall(r'"([a-z0-9-]+)"', call))
    return sorted(referenced - set(ICONS)), sorted(set(ICONS) - referenced)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify without writing")
    arguments = parser.parse_args()

    html = INDEX.read_text()
    if START not in html or END not in html:
        raise SystemExit(f"markers {START} / {END} not found in {INDEX}")
    before, _, rest = html.partition(START)
    _, _, after = rest.partition(END)
    sprite = build()
    updated = f"{before}{START}{sprite}{END}{after}"

    def report() -> None:
        missing, unused = audit_references()
        if missing:
            sys.exit("referenced but not in the sprite: "
                     + ", ".join(f"i-{name}" for name in missing))
        if unused:
            print("warning: in the sprite but never referenced: "
                  + ", ".join(f"i-{name}" for name in unused))

    if arguments.check:
        if updated != html:
            sys.exit("sprite in index.html is stale -- rerun tools/build_icons.py")
        report()
        print(f"sprite matches the package ({len(ICONS)} icons), all references resolve")
        return
    INDEX.write_text(updated)
    report()
    print(f"wrote {len(ICONS)} icons into {INDEX.relative_to(ROOT)}; all references resolve")


if __name__ == "__main__":
    main()
