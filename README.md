# Telegram Music

Telegram is a surprisingly good place to collect music and a fairly awkward place to listen to it. Telegram Music turns the audio in your channels, bots, private chats, and Saved Messages into a proper personal library in the browser.

This is a private, single-user app. It connects to your own Telegram account, streams files without making you export them first, and keeps its own local layer for likes, lyrics, artwork, and metadata edits.

## What it does

- Builds playlists from selected Telegram channels, bots, private chats, and Saved Messages.
- Searches Telegram live, including sources that are not currently in the sidebar.
- Handles large libraries with paginated data, virtualized rows, lazy artwork, and no track-count cap.
- Keeps a persistent queue with weighted shuffle, repeat modes, listening history, and resumable prefetching.
- Saves likes locally and can send a track to Saved Messages or forward it to a Telegram contact.
- Fetches metadata and high-resolution covers from MusicBrainz and Cover Art Archive.
- Finds synced or plain lyrics through LRCLIB, with a built-in editor for manual lyrics and LRC timestamps.
- Stores metadata edits separately from Telegram, so a resync never overwrites your changes.
- Downloads the original audio, or writes your local metadata into the downloaded copy when FFmpeg is available.

Removing a source only removes it from this app. It does not leave the channel, delete the chat, or change anything in Telegram.

## Quick start

You will need Python 3.12 or newer and [uv](https://docs.astral.sh/uv/). FFmpeg is optional and is only needed to write edited metadata into downloaded files.

1. Install the dependencies:

   ```sh
   cp .env.example .env
   uv sync
   ```

2. Start the player:

   ```sh
   uv run uvicorn app:app --reload
   ```

3. Open [http://localhost:8000](http://localhost:8000) and connect Telegram using QR login or your phone number. Two-step verification is supported.

The first sync can take a while for large channels. It runs in the background and shows how many messages were checked and how many tracks were found.

## Metadata and lyrics

MusicBrainz asks clients to identify themselves. Open Settings → Metadata and enter an email address or website you control. This is a contact string, not an API key. You can also set it with `MUSICBRAINZ_CONTACT` in `.env`.

Fetched metadata is presented as a choice before it is applied. Titles, artists, albums, covers, and manual changes live in the local database and take precedence over Telegram's embedded tags. Lyrics work the same way: fetched or edited lyrics are cached locally and remain available between sessions.

If FFmpeg is installed, downloading a track with local metadata edits creates a separate tagged copy. The cached Telegram file is never modified. Without FFmpeg, downloads still work but keep their original embedded tags.

## Running with Docker

The included Compose setup keeps the library and media cache in `./data` and exposes the app on port 8000:

```sh
cp .env.example .env
docker compose up -d --build
```

In production, put the app behind HTTPS.

The provided image stays small and does not install FFmpeg. Extend the image with FFmpeg if you want downloaded files to include local metadata edits.

## Data and security

The app stores its SQLite library, cached artwork, prefetched audio, and encrypted Telegram authorization under `DATA_DIR`. Treat that directory, `.env`, and any backups as private.

The Telegram session is encrypted at rest with `APP_ENCRYPTION_KEY`, but the key and database together are enough to recover it. Keep them separate from public backups. Changing the encryption key makes the stored session unreadable and requires reconnecting.

This is designed for one trusted owner, not as a public multi-user service. Serve it over HTTPS outside local development and apply sensible network access controls.

## Checks

```sh
uv run python -m unittest discover -s tests
node --test static/player-core.test.js
```

## Acknowledgements

The interface uses icons derived from [Tabler Icons](https://tabler.io/icons) under the MIT License. Telegram Desktop, PixelPlayer, and Better Lyrics were studied as product and interaction references; their source code and visual assets were not copied into this project.
