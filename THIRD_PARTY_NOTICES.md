# Third-party notices

## Icons

Interface icons are [Hugeicons](https://hugeicons.com/) Stroke Rounded icons, supplied by the
MIT-licensed [`@hugeicons/core-free-icons`](https://www.npmjs.com/package/@hugeicons/core-free-icons)
package (outline weight, plus filled `play` and `heart` variants for the primary transport button
and liked state).

They are inlined as `<symbol>` elements in `static/index.html` rather than loaded through a web
font or framework wrapper. This keeps the app's `script-src 'self'` CSP intact, avoids a network
dependency before first paint, and keeps the local-first player usable offline. The Hugeicons
package is a build-time source only — regenerate the sprite with `python3 tools/build_icons.py`.

Hugeicons package license: https://www.npmjs.com/package/@hugeicons/core-free-icons

## Python dependencies

Installed from PyPI and pinned in `uv.lock`; each carries its own license:

- [Telethon](https://github.com/LonamiWebs/Telethon) — MIT
- [FastAPI](https://github.com/fastapi/fastapi) — MIT
- [uvicorn](https://github.com/encode/uvicorn) — BSD-3-Clause
- [httpx](https://github.com/encode/httpx) — BSD-3-Clause
- [cryptography](https://github.com/pyca/cryptography) — Apache-2.0 OR BSD-3-Clause
- [segno](https://github.com/heuer/segno) — BSD-3-Clause

## Metadata and artwork services

Optional lookups query [MusicBrainz](https://musicbrainz.org/) and
[Cover Art Archive](https://coverartarchive.org/), whose data is used under their own terms.
MusicBrainz requires a contact address in the User-Agent, which is why `MUSICBRAINZ_CONTACT`
is required for those features.
