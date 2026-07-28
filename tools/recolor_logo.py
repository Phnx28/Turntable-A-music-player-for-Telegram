"""Recolour the brand logo to a neutral ink tone for each theme.

The source artwork is pure magenta (#ff00ff) -- a placeholder key colour, not a brand colour.
Rendering it as-is showed a purple logo in light mode, and the stylesheet's `filter: invert(1)`
turned that into green in dark mode (inverting a saturated hue gives its complement).

The shape lives in the alpha channel, so the mark can be recoloured by replacing RGB while
keeping alpha exactly as-is -- antialiased edges survive untouched. Relative luminance is
preserved so highlight details (the white specular on the tonearm) stay lighter than the body.

Pillow is not a runtime dependency of the app, so run this with the system Python:

    python3 tools/recolor_logo.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
# The untouched magenta original. Reading from here (never from static/) keeps this script
# idempotent -- rerunning it cannot recolour an already-recoloured file.
SOURCE = ROOT / "assets-src" / "logo-source.png"

# Matches --ink in style.css for each theme, so the logo reads as part of the UI text.
LIGHT_INK = (17, 17, 17)  # #111111
DARK_INK = (242, 242, 238)  # #f2f2ee


def recolour(source: Path, target: Path, ink: tuple[int, int, int]) -> None:
    image = Image.open(source).convert("RGBA")
    pixels = list(image.getdata())
    out: list[tuple[int, int, int, int]] = []
    for red, green, blue, alpha in pixels:
        if alpha == 0:
            # Keep fully transparent pixels neutral so no magenta fringe can bleed through
            # when the image is scaled.
            out.append((0, 0, 0, 0))
            continue
        # Magenta body vs. white highlight: whites have a high green channel, the magenta has
        # almost none. Use green as the discriminator and keep highlights proportionally lighter.
        highlight = green / 255
        shade = tuple(round(channel + (255 - channel) * highlight) for channel in ink)
        out.append((shade[0], shade[1], shade[2], alpha))
    result = Image.new("RGBA", image.size)
    result.putdata(out)
    # The mark is two tones plus alpha, so a palette beats full RGBA by roughly half the bytes.
    # quantize() keeps the alpha channel; RGBA is retained only if the palette would lose detail.
    palette = result.quantize(colors=64, method=Image.Quantize.FASTOCTREE)
    candidate = target.with_suffix(".palette.png")
    palette.save(candidate, optimize=True)
    result.save(target, optimize=True)
    if candidate.stat().st_size < target.stat().st_size:
        candidate.replace(target)
    else:
        candidate.unlink()
    size = target.stat().st_size
    print(f"wrote {target.relative_to(ROOT)}  ink=#{ink[0]:02x}{ink[1]:02x}{ink[2]:02x}  {size:,}B")


def main() -> None:
    # Both outputs derive from one source so the two themes cannot drift apart.
    recolour(SOURCE, STATIC / "logo-light.png", LIGHT_INK)
    recolour(SOURCE, STATIC / "logo-dark.png", DARK_INK)
    # PWA icons carried the same magenta cast. They sit on the manifest's light background
    # (#f7f7f4) in the installer and on the home screen, so both use the dark ink.
    for size in (192, 512):
        recolour(ROOT / "assets-src" / f"icon-{size}-source.png", STATIC / f"icon-{size}.png", LIGHT_INK)


if __name__ == "__main__":
    main()
