# Telegram Music

Web player for audio in your Telegram chats. Single-user, self-hosted.

## Quick start

```sh
cp .env.example .env
uv sync
uv run uvicorn app:app --reload
```

Open http://localhost:8000, connect Telegram via QR or phone.

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
