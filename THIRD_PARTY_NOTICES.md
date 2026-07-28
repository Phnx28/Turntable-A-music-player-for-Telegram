# Third-party notices

## Icons

Interface icons are [Ionicons](https://ionic.io/ionicons) by Ionic, licensed under the
[MIT License](https://github.com/ionic-team/ionicons/blob/main/LICENSE) (outline weight, plus
the filled `play` and `heart` for the primary transport button and the liked state).

They are inlined as `<symbol>` elements in `static/index.html` rather than loaded through
Ionicons' web component: that component is an ESM bundle fetched from a CDN, which the app's
`script-src 'self'` CSP blocks, and it would put a network dependency in front of the first
paint of a local-first player. `ionicons` in `package.json` is a build-time source only —
regenerate the sprite with `python3 tools/build_icons.py`.

Ionicons license: https://github.com/ionic-team/ionicons/blob/main/LICENSE

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
