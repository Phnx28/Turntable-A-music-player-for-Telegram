# Telegram Turntable

A self-hosted web music player for audio you already have in Telegram.

Point it at the chats, channels and bots holding your audio and it indexes them into a local
library with a player on top, streaming each file from Telegram on demand rather than keeping a
second copy of your collection.

A fun side project, not a product. Single user, single machine.

## What it does

- **Indexes any chat, channel, bot or Saved Messages** you choose, and syncs new tracks
  incrementally afterwards.
- **Streams on demand** with seeking, plus a local cache so replays are instant.
- **Library** with search, shuffle, a queue, likes and play-position memory.
- **Fills in missing metadata** from [MusicBrainz](https://musicbrainz.org), cover art from
  [Cover Art Archive](https://coverartarchive.org), and synced lyrics from
  [LRCLIB](https://lrclib.net) — all editable and overridable by hand.
- **Sends tracks back to Telegram**: to Saved Messages, or forwarded to a contact (frequent
  recipients first).
- **Installable as a PWA**, with lock-screen controls and artwork via MediaSession.

## Requirements

Python 3.12+ and [uv](https://docs.astral.sh/uv/). That is all — the frontend is plain
JavaScript with no build step.

## Install

```sh
cp .env.example .env
uv sync
uv run python run.py
```

Open <http://localhost:8000> and link Telegram by scanning the QR code with your phone
(Telegram → Settings → Devices → Link Desktop Device), or by phone number.

Edit `.env` and set `MUSICBRAINZ_CONTACT` to an email address you control — MusicBrainz rejects
anonymous requests, so metadata lookups fail without it.

Run via `run.py`, not `uvicorn` directly: it reads the bind address you chose in Settings.
Add `--reload` while developing.

### Docker

```sh
cp .env.example .env
docker compose up -d --build
```

## Security

**By default the player binds to `127.0.0.1` and has no password**, which is safe on a personal
machine. Both are in Settings → Network.

Anyone who reaches this app is logged into your Telegram: they can read your library, see your
contacts, and send messages as you. **Set a password before binding to `0.0.0.0`.** Sessions
last 30 days; changing or removing the password signs every other browser out.

There is no HTTPS here. Over anything other than localhost, put it behind a reverse proxy or a
VPN — the session cookie is only marked `Secure` when the request already arrived over HTTPS.

## Your data

Everything lives in `DATA_DIR` (`./data` by default): the SQLite library, the media cache, and
your encrypted Telegram session. The encryption key is generated on first run and stored
alongside it — set `APP_ENCRYPTION_KEY` in `.env` to keep the key out of the directory it
decrypts.

**Back up `DATA_DIR`.** Likes, manual lyrics, metadata overrides and play positions exist
nowhere else; tracks themselves are still safe in Telegram.

## API credentials

A Telegram API registration is built in, so the app works out of the box. Every install that
uses it shares one `api_id`, which Telegram may rate-limit. To use your own, register at
[my.telegram.org](https://my.telegram.org) and set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`
together in `.env` — half a pair is rejected at startup rather than silently mixed.

## Development

```sh
uv run python -m unittest discover -s tests   # backend
node --test static/player-core.test.js        # player logic
```

The icon sprite in `static/index.html` is generated — edit `tools/build_icons.py` and rerun it,
never the sprite by hand:

```sh
npm install                      # Ionicons, build-time only
python3 tools/build_icons.py     # --check verifies without writing
```

Agents and contributors: see [`memory.md`](memory.md) for accumulated gotchas.

## License

MIT — see [LICENSE](LICENSE). Icons are [Ionicons](https://ionic.io/ionicons) (MIT); see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
