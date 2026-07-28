# Third-party notices

## Icons

Interface icons are derived from the [Solar](https://www.figma.com/community/file/1166831539721848736)
icon set (bold-duotone weight) by 480 Design, licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

The icons are inlined as `<symbol>` elements in `static/index.html` rather than loaded from a
package at runtime; `@iconify-json/solar` in `package.json` is the build-time source they were
taken from. `#i-pin` is original work and not part of the set.

CC BY 4.0 requires attribution, so this notice must be kept if the icons are.

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
