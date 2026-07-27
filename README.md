# Telegram Turntable

Web player for audio in your Telegram chats. Single-user, self-hosted.

## Quick start

```sh
cp .env.example .env
uv sync
uv run python run.py
```

Open http://localhost:8000, connect Telegram via QR or phone.

Use `run.py` rather than calling uvicorn directly — it reads the bind address you
chose in Settings → Network. Add `--reload` while developing.

## Access

By default the player listens on `127.0.0.1`, so only this machine can reach it,
and it has **no password**. Both are configurable in Settings → Network:

- **Who can reach this player** — `127.0.0.1` (this machine only) or `0.0.0.0`
  (anyone on your network). Takes effect after a restart.
- **Password** — off by default. When on, the player asks for it before loading.
  Sessions last 30 days; changing or removing the password signs out every
  other browser.

If you switch to `0.0.0.0`, anyone who can reach the port can read your library,
see your contacts, and forward tracks as you. Set a password first.

## Docker

```sh
cp .env.example .env
docker compose up -d --build
```

## Checks

```sh
uv run python -m unittest discover -s tests
node --test static/player-core.test.js
```
