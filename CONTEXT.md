# Telegram Turntable

This context names the music library concepts shared by Telegram ingestion, browsing, and playback. It keeps domain language stable while the implementation evolves.

## Library

**Track**:
An audio item indexed from Telegram and made available to the player when its media is available.
_Avoid_: song, file

**Source**:
A Telegram chat, channel, bot, private conversation, or Saved Messages collection from which tracks are indexed.
_Avoid_: playlist, provider

**Library**:
The locally indexed collection of tracks from the sources selected for this player.
_Avoid_: catalog, database

**Library view**:
A filtered, ordered view of the library shown to the listener, such as All music, a source, or Liked songs.
_Avoid_: page, result set

**Day break**:
A chronological marker separating tracks from different UTC calendar days in the All music library view.
_Avoid_: date header, day separator

**Media cache**:
The on-disk copy of a Track's audio that replays and seeks are served from, downloaded in the background while the Track streams.
_Avoid_: cache, buffer, storage
