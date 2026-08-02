"""Rebuild the inlined icon sprite in static/index.html from Hugeicons.

The icons are inlined as <symbol> elements rather than loaded through a web font or a
framework wrapper. Inlining keeps one request, keeps `currentColor` working, and keeps the
app usable offline.

Usage (Hugeicons is a build-time source only, not a runtime dependency):

    npm install @hugeicons/core-free-icons
    python3 tools/build_icons.py

Pass --check to verify the sprite in index.html matches the package without writing.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "static" / "index.html"
ICON_EXPORTS: dict[str, str] = {
    "play-filled": "PlayIcon",
    "pause": "PauseIcon",
    # Previous/next, not back/forward: these buttons jump between tracks.
    "prev": "PreviousIcon",
    "next": "NextIcon",
    "volume": "VolumeHighIcon",
    "search": "Search01Icon",
    "sync": "RefreshIcon",
    "plus": "Add01Icon",
    "more": "MoreHorizontalIcon",
    "close": "Cancel01Icon",
    "download": "Download01Icon",
    "edit": "Edit02Icon",
    "menu": "Menu01Icon",
    "lyrics": "MusicNote01Icon",
    "shuffle": "ShuffleIcon",
    "repeat": "RepeatIcon",
    "collapse": "ArrowLeft01Icon",
    "heart": "HeartIcon",
    # The free package is stroke-rounded only. Reuse the same closed Hugeicons heart path,
    # filled, so the active liked state remains visibly distinct without adding a second set.
    "heart-filled": "HeartIcon",
    "bookmark": "Bookmark01Icon",
    "send": "SentIcon",
    "locate": "Location01Icon",
    "pin": "PinIcon",
}

FILLED = {"play-filled", "heart-filled"}

START = "<!-- icons:start -->"
END = "<!-- icons:end -->"


def package_icons() -> dict[str, list[list[object]]]:
    """Read the package's raw SVG element tuples without adding a runtime JS dependency."""
    script = """
import * as icons from '@hugeicons/core-free-icons';
const names = JSON.parse(process.argv[1]);
const missing = Object.values(names).filter((name) => !icons[name]);
if (missing.length) throw new Error(`missing Hugeicons exports: ${missing.join(', ')}`);
process.stdout.write(JSON.stringify(Object.fromEntries(
  Object.entries(names).map(([id, name]) => [id, icons[name]])
)));
"""
    try:
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script, json.dumps(ICON_EXPORTS)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        details = getattr(error, "stderr", "") or str(error)
        raise SystemExit(
            "Unable to read @hugeicons/core-free-icons; run `npm install "
            "@hugeicons/core-free-icons` first.\n" + details.strip()
        ) from error
    return json.loads(result.stdout)


def svg_attribute(name: str) -> str:
    return re.sub(r"(?<!^)([A-Z])", r"-\1", name).lower()


def symbol(icon_id: str, elements: list[list[object]]) -> str:
    """Convert Hugeicons' raw SVG element tuples into one sprite symbol."""
    children = []
    for tag, raw_attributes in elements:
        attributes = {
            svg_attribute(str(name)): str(value)
            for name, value in dict(raw_attributes).items()
        }
        attributes.pop("key", None)
        if icon_id in FILLED and tag == "path":
            attributes["fill"] = "currentColor"
            attributes["stroke"] = "none"
            attributes.pop("stroke-linecap", None)
            attributes.pop("stroke-linejoin", None)
            attributes.pop("stroke-width", None)
        elif "stroke" in attributes and "fill" not in attributes:
            attributes["fill"] = "none"
        rendered = " ".join(
            f'{name}="{html.escape(value, quote=True)}"'
            for name, value in attributes.items()
        )
        children.append(f"<{tag} {rendered}/>")
    return f'<symbol id="i-{icon_id}" viewBox="0 0 24 24">{"".join(children)}</symbol>'


def build() -> str:
    icons = package_icons()
    return "".join(symbol(icon_id, icons[icon_id]) for icon_id in ICON_EXPORTS)


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
    return sorted(referenced - set(ICON_EXPORTS)), sorted(set(ICON_EXPORTS) - referenced)


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
        print(f"sprite matches Hugeicons ({len(ICON_EXPORTS)} icons), all references resolve")
        return
    INDEX.write_text(updated)
    report()
    print(f"wrote {len(ICON_EXPORTS)} Hugeicons into {INDEX.relative_to(ROOT)}; all references resolve")


if __name__ == "__main__":
    main()
