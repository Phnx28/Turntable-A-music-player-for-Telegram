from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import segno
import httpx
from cryptography.fernet import Fernet, InvalidToken
from telethon import TelegramClient, events, functions, types, utils
from telethon.errors import FloodWaitError, RPCError, SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    InputMessagesFilterMusic,
)
from telethon.tl.types import contacts as contacts_types

from core import Database, is_audio_file, media_identity, normalize_text, track_key
from jobs import BackgroundJob, JobRunner
from media import MediaCache

LOGGER = logging.getLogger(__name__)


FLOW_TTL_SECONDS = 300
# How many frequent-forward peers to ask Telegram for. Only the handful that are also saved
# contacts get shown, so this is deliberately larger than the row can hold.
TOP_PEER_LIMIT = 20
# Corner radius of each QR module, as a fraction of the module size. Kept well under .5 (a full
# circle) so the modules stay square enough for scanners to read reliably.
QR_MODULE_RADIUS = 0.28
# Blank margin around the code, in modules. ISO/IEC 18004 asks for 4; measured against OpenCV,
# 0 never decodes and 2 is already enough, so 3 sits comfortably above the floor while costing
# less of a small box than 4 would.
#
# It belongs inside the viewBox rather than in CSS padding. A fixed 11px padding is a shrinking
# share of the code as the box grows -- 1.42 modules at the rendered 224px, 0.46 at 700px -- so
# the margin silently depended on the display size. Honest note: OpenCV still decoded the old
# rendering at every size tested, including blurred, rotated and noisy variants, so this fixes a
# latent fragility against stricter scanners rather than a reproduced failure.
QR_QUIET_MODULES = 3


def render_qr_svg(payload: str) -> str:
    """Render *payload* as a responsive SVG with softly rounded modules.

    segno's own writers emit a fixed pixel width with no viewBox, so the markup cannot scale to
    its container -- the artwork stays at its natural size and sits off-centre in a larger box.
    Emitting our own path with a viewBox makes the code scale to whatever the stylesheet asks for
    and land dead centre.

    Each module is drawn as a square whose four corners are rounded only where both adjoining
    neighbours are blank. Rounding every corner unconditionally would carve notches out of solid
    regions like the finder squares; leaving shared edges square keeps runs looking welded.
    """
    # No explicit error level: segno picks the smallest version that fits, and forcing a higher
    # level here would silently add modules (33 -> 37 for a login URL), shrinking each one.
    matrix = segno.make(payload).matrix
    size = len(matrix)
    r = round(QR_MODULE_RADIUS, 4)

    def dark(x: int, y: int) -> bool:
        return 0 <= x < size and 0 <= y < size and bool(matrix[y][x])

    parts: list[str] = []
    for row in range(size):
        for column in range(size):
            if not matrix[row][column]:
                continue
            up, down = dark(column, row - 1), dark(column, row + 1)
            left, right = dark(column - 1, row), dark(column + 1, row)
            # Round a corner only where both of its adjoining neighbours are blank.
            tl, tr = not (up or left), not (up or right)
            br, bl = not (down or right), not (down or left)
            # Neighbour lookups stay in matrix space; only the drawing origin shifts, so the
            # quiet zone cannot affect which corners get rounded.
            x, y = column + QR_QUIET_MODULES, row + QR_QUIET_MODULES
            right_edge = x + 1
            parts.append(f"M{x + (r if tl else 0)} {y}")
            parts.append(f"H{right_edge - r}" if tr else f"H{right_edge}")
            if tr:
                parts.append(f"A{r} {r} 0 0 1 {right_edge} {y + r}")
            parts.append(f"V{y + 1 - r}" if br else f"V{y + 1}")
            if br:
                parts.append(f"A{r} {r} 0 0 1 {right_edge - r} {y + 1}")
            parts.append(f"H{x + r}" if bl else f"H{x}")
            if bl:
                parts.append(f"A{r} {r} 0 0 1 {x} {y + 1 - r}")
            parts.append(f"V{y + r}" if tl else f"V{y}")
            if tl:
                parts.append(f"A{r} {r} 0 0 1 {x + r} {y}")
            parts.append("Z")
    # The white rect spans the padded box, so the quiet zone is part of the image and survives
    # being placed on a dark surface.
    extent = size + QR_QUIET_MODULES * 2
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {extent} {extent}" '
        f'shape-rendering="geometricPrecision" role="img">'
        f'<path fill="#fff" d="M0 0h{extent}v{extent}H0z"/>'
        f'<path fill="#111111" d="{"".join(parts)}"/>'
        "</svg>"
    )


@dataclass
class LoginFlow:
    id: str
    kind: str
    client: TelegramClient
    created_at: float = field(default_factory=time.monotonic)
    state: str = "waiting"
    phone: str = ""
    phone_code_hash: str = ""
    qr: Any = None
    task: asyncio.Task[Any] | None = None
    error: str = ""


class TelegramService:
    def __init__(
        self,
        database: Database,
        *,
        api_id: int,
        api_hash: str,
        encryption_key: str,
        data_directory: Path,
    ):
        self.database = database
        self.api_id = api_id
        self.api_hash = api_hash
        self.fernet = Fernet(encryption_key.encode())
        self.client: TelegramClient | None = None
        self.flows: dict[str, LoginFlow] = {}
        # The job runner owns every background task this service spawns; stop() cancels it all.
        self.jobs = JobRunner()
        # ponytail: per-source locks beat one global lock; same chat_id can't double-sync, different ones run in parallel.
        self.sync_locks: dict[str, asyncio.Lock] = {}
        # ponytail: caps concurrent scans to avoid FloodWait; bump if profiling shows idle headroom.
        self.sync_semaphore = asyncio.Semaphore(3)
        self.global_search_lock = asyncio.Lock()
        self.avatar_directory = data_directory / "avatars"
        self.thumbnail_directory = data_directory / "thumbnails"
        self.avatar_directory.mkdir(parents=True, exist_ok=True)
        self.thumbnail_directory.mkdir(parents=True, exist_ok=True)
        # Auto cover-art enrichment. Set by create_app once ExternalServices exists; the
        # worker is the same candidate-lookup + artwork-writer the metadata dialog uses.
        self.enrich_worker: Callable[[bool], Awaitable[dict[str, Any]]] | None = None
        self.prefetch_keys: set[str] = set()
        self.prefetch_order: list[str] = []
        self.discovery_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._watched_chat_ids: set[str] = set()
        self._countries: list[dict[str, str]] | None = None
        self._countries_updated: float = 0
        # The media cache owns the .part protocol, eviction, and byte sourcing. It shares the
        # prefetch-key set by reference so eviction never deletes a track being prefetched.
        self.media = MediaCache(
            database,
            media_directory=data_directory / "media-cache",
            download_directory=data_directory / "tagged-downloads",
            client_provider=lambda: self.client,
            protected_keys=self.prefetch_keys,
        )

    @property
    def linked(self) -> bool:
        return self.client is not None and self.client.is_connected()

    async def start(self) -> None:
        account = self.database.get_account()
        if not account:
            return
        try:
            session = self.fernet.decrypt(account["encrypted_session"]).decode()
        except (InvalidToken, UnicodeDecodeError) as error:
            raise RuntimeError("Stored Telegram session cannot be decrypted") from error
        client = self._new_client(session)
        try:
            await client.connect()
            authorized = await client.is_user_authorized()
        except Exception:
            await client.disconnect()
            raise
        if not authorized:
            await client.disconnect()
            self.database.clear_account()
            return
        self.client = client
        self._refresh_watched_ids()
        self._install_handlers(client)
        self.jobs.spawn(self.sync_all())

    async def stop(self) -> None:
        await self.jobs.cancel_all()
        await self.media.shutdown()
        for flow in list(self.flows.values()):
            if flow.task:
                flow.task.cancel()
            await flow.client.disconnect()
        self.flows.clear()
        if self.client:
            await self.client.disconnect()
            self.client = None

    def job_status(self, job_id: str) -> dict[str, Any]:
        return self.jobs.status(job_id)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return self.jobs.cancel_by_id(job_id)

    def _new_client(self, session: str = "") -> TelegramClient:
        return TelegramClient(
            StringSession(session),
            self.api_id,
            self.api_hash,
            connection_retries=5,
            request_retries=3,
            flood_sleep_threshold=30,
            device_model="Telegram Turntable Web",
            app_version="1.0.0",
        )

    def _install_handlers(self, client: TelegramClient) -> None:
        client.add_event_handler(self._on_new_message, events.NewMessage())
        client.add_event_handler(self._on_edited_message, events.MessageEdited())
        client.add_event_handler(self._on_deleted_message, events.MessageDeleted())

    def _refresh_watched_ids(self) -> None:
        # ponytail: full rescan on change is fine; set is small (<10 chats), add per-source subscribe when this grows.
        self._watched_chat_ids = {source["chatId"] for source in self.database.list_sources(False)}

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        await self._save_event_message(event)

    async def _on_edited_message(self, event: events.MessageEdited.Event) -> None:
        await self._save_event_message(event)

    async def _save_event_message(self, event: Any) -> None:
        chat_id = str(event.chat_id or "")
        if not chat_id or chat_id not in self._watched_chat_ids:
            return
        source = self.database.get_source(chat_id) if chat_id else None
        if not source or (not source["selected"] and source["kind"] not in {"channel", "bot"}):
            return
        item = self._message_to_track(event.message, chat_id)
        if item:
            self.database.upsert_tracks([item])
            if source["selected"]:
                self.database.finish_sync(chat_id, int(event.message.id))

    async def _on_deleted_message(self, event: events.MessageDeleted.Event) -> None:
        chat_id = str(event.chat_id or "")
        if not chat_id or chat_id not in self._watched_chat_ids:
            return
        source = self.database.get_source(chat_id) if chat_id else None
        if source and (source["selected"] or source["kind"] in {"channel", "bot"}):
            self.database.mark_unavailable(chat_id, [str(value) for value in event.deleted_ids])

    async def _discard_flows(self) -> None:
        flows, self.flows = list(self.flows.values()), {}
        for flow in flows:
            if flow.task:
                flow.task.cancel()
        await asyncio.gather(*(flow.task for flow in flows if flow.task), return_exceptions=True)
        for flow in flows:
            if flow.client is not self.client:
                await flow.client.disconnect()

    async def start_qr_login(self) -> dict[str, Any]:
        await self._discard_flows()
        client = self._new_client()
        await client.connect()
        qr = await client.qr_login()
        flow = LoginFlow(secrets.token_urlsafe(24), "qr", client, qr=qr)
        self.flows[flow.id] = flow
        flow.task = asyncio.create_task(self._wait_for_qr(flow))
        svg = render_qr_svg(qr.url)
        return {"flowId": flow.id, "svg": svg, "expiresAt": int(qr.expires.timestamp())}

    async def _wait_for_qr(self, flow: LoginFlow) -> None:
        try:
            await flow.qr.wait(timeout=60)
            await self._complete_flow(flow)
        except SessionPasswordNeededError:
            flow.state = "password_required"
        except asyncio.TimeoutError:
            flow.state = "expired"
            await flow.client.disconnect()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            flow.error = self._friendly_error(error)
            flow.state = "error"
            await flow.client.disconnect()

    async def start_phone_login(self, phone: str) -> dict[str, Any]:
        await self._discard_flows()
        if not phone.startswith("+") or not phone[1:].replace(" ", "").isdigit():
            raise ValueError("Enter the phone number in international format")
        client = self._new_client()
        await client.connect()
        try:
            sent = await client.send_code_request(phone)
        except Exception:
            await client.disconnect()
            raise
        flow = LoginFlow(
            secrets.token_urlsafe(24),
            "phone",
            client,
            phone=phone,
            phone_code_hash=sent.phone_code_hash,
        )
        self.flows[flow.id] = flow
        delivery = type(sent.type).__name__.removeprefix("SentCodeType")
        return {"flowId": flow.id, "delivery": delivery, "state": flow.state}

    async def submit_phone_code(self, flow_id: str, code: str) -> dict[str, Any]:
        flow = self._flow(flow_id, "phone")
        if not code.strip():
            raise ValueError("Enter the Telegram login code")
        flow.error = ""
        try:
            await flow.client.sign_in(
                phone=flow.phone,
                code=code.strip(),
                phone_code_hash=flow.phone_code_hash,
            )
            await self._complete_flow(flow)
        except SessionPasswordNeededError:
            flow.state = "password_required"
        except Exception as error:
            flow.error = self._friendly_error(error)
            flow.state = "expired" if "expired" in flow.error else "waiting"
        return self.flow_status(flow_id)

    async def submit_password(self, flow_id: str, password: str) -> dict[str, Any]:
        flow = self._flow(flow_id)
        if flow.state != "password_required":
            raise ValueError("This login is not waiting for a password")
        flow.error = ""
        try:
            await flow.client.sign_in(password=password)
            await self._complete_flow(flow)
        except Exception as error:
            flow.error = self._friendly_error(error)
            flow.state = "password_required"
        return self.flow_status(flow_id)

    def _flow(self, flow_id: str, kind: str | None = None) -> LoginFlow:
        flow = self.flows.get(flow_id)
        if not flow or (kind and flow.kind != kind):
            raise KeyError("Login flow expired")
        if time.monotonic() - flow.created_at > FLOW_TTL_SECONDS:
            raise KeyError("Login flow expired")
        return flow

    def flow_status(self, flow_id: str) -> dict[str, Any]:
        flow = self._flow(flow_id)
        return {"state": flow.state, "error": flow.error or None}

    async def _complete_flow(self, flow: LoginFlow) -> None:
        user = await flow.client.get_me()
        if not user:
            raise RuntimeError("Telegram did not return the signed-in account")
        account = self.database.get_account()
        if account and account["telegram_user_id"] != str(user.id):
            await flow.client.log_out()
            flow.state = "error"
            flow.error = "This installation is linked to a different Telegram account"
            return
        if self.client and self.client is not flow.client:
            await self.client.disconnect()
        session = flow.client.session.save()
        encrypted = self.fernet.encrypt(session.encode())
        display_name = utils.get_display_name(user) or "Telegram account"
        self.database.set_account(str(user.id), display_name, encrypted)
        self.client = flow.client
        self._refresh_watched_ids()
        self._install_handlers(flow.client)
        flow.error = ""
        flow.state = "ready"
        self.jobs.spawn(self.sync_all())

    async def countries(self) -> list[dict[str, str]]:
        if self._countries is not None and time.monotonic() - self._countries_updated < 3600:
            return self._countries
        client = self._new_client()
        await client.connect()
        try:
            result = await client(functions.help.GetCountriesListRequest(lang_code="en", hash=0))
        finally:
            await client.disconnect()
        countries = [
            {"iso2": country.iso2, "name": country.name or country.default_name, "dialCode": code.country_code}
            for country in result.countries
            if not country.hidden
            for code in country.country_codes
        ]
        self._countries = sorted(countries, key=lambda item: (item["name"].casefold(), item["dialCode"]))
        self._countries_updated = time.monotonic()
        return self._countries

    @staticmethod
    def _friendly_error(error: Exception) -> str:
        if isinstance(error, FloodWaitError):
            return f"Telegram asked this login to wait {error.seconds} seconds"
        message = getattr(error, "message", "") or str(error)
        known = {
            "PHONE_CODE_INVALID": "The Telegram login code is incorrect",
            "PHONE_CODE_EXPIRED": "The Telegram login code expired; start again",
            "PASSWORD_HASH_INVALID": "The Telegram 2FA password is incorrect",
            "PHONE_NUMBER_INVALID": "The phone number is invalid",
        }
        for key, value in known.items():
            if key in message:
                return value
        return "Telegram could not complete the login. Try QR login or start again."

    async def disconnect_account(self) -> None:
        if self.client:
            try:
                await self.client.log_out()
            finally:
                self.client = None
        self.database.clear_account()
        self.clear_media_cache()
        self.database.clear_sources()

    def account_status(self) -> dict[str, Any]:
        account = self.database.get_account()
        return {
            "linked": bool(account and self.linked),
            "userId": account["telegram_user_id"] if account else None,
            "displayName": account["display_name"] if account else None,
        }

    def require_client(self) -> TelegramClient:
        if not self.client or not self.client.is_connected():
            raise RuntimeError("Link Telegram before using the library")
        return self.client

    @staticmethod
    def classify_entity(entity: Any) -> str | None:
        if isinstance(entity, types.User):
            if entity.is_self:
                return "saved"
            return "bot" if entity.bot else "private"
        if isinstance(entity, types.Channel) and not entity.megagroup and not getattr(entity, "gigagroup", False):
            return "channel"
        return None

    async def discover_sources(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self.discovery_cache and now - self.discovery_cache[0] < 60:
            known = {source["chatId"]: source for source in self.database.list_sources(False)}
            return [
                {
                    **source,
                    "selected": bool(known.get(source["chatId"], {}).get("selected")),
                    "trackCount": int(known.get(source["chatId"], {}).get("trackCount", 0)),
                }
                for source in self.discovery_cache[1]
            ]
        client = self.require_client()
        known = {source["chatId"]: source for source in self.database.list_sources(False)}
        discovered: list[dict[str, Any]] = []
        # asyncio.wait_for() takes an awaitable, but iter_dialogs() returns an async ITERATOR, so
        # the old wrapping raised TypeError and broke discovery outright. Bound the whole walk with
        # a deadline instead, which is what the 60s was for.
        discovery_deadline = time.monotonic() + 60
        async for dialog in client.iter_dialogs():
            if time.monotonic() > discovery_deadline:
                break
            kind = self.classify_entity(dialog.entity)
            if not kind:
                continue
            discovered.append(
                {
                    "chatId": str(dialog.id),
                    "kind": kind,
                    "title": dialog.name or "Untitled chat",
                    "username": getattr(dialog.entity, "username", None),
                    "selected": bool(known.get(str(dialog.id), {}).get("selected")),
                    "lastPostAt": int(dialog.date.timestamp()) if dialog.date else None,
                    "trackCount": int(known.get(str(dialog.id), {}).get("trackCount", 0)),
                    "avatarUrl": f"/api/sources/{dialog.id}/avatar",
                }
            )
        discovered = sorted(discovered, key=lambda item: (item["kind"], item["title"].casefold()))
        self.discovery_cache = (now, [{**item} for item in discovered])
        return discovered

    async def global_music_search(self, query: str, limit: int = 30) -> dict[str, Any]:
        cleaned = query.strip()[:200]
        if len(cleaned) < 3:
            raise ValueError("Enter at least three characters to search Telegram")
        limit = max(1, min(int(limit), 50))
        async with self.global_search_lock:
            client = self.require_client()
            known = {
                source["chatId"]: source
                for source in self.database.list_sources(False)
            }
            track_sources: dict[str, dict[str, Any]] = {}
            tracks: list[dict[str, Any]] = []
            async for message in client.iter_messages(
                None,
                search=cleaned,
                filter=InputMessagesFilterMusic(),
                limit=limit * 4,
            ):
                entity = getattr(message, "chat", None)
                kind = self.classify_entity(entity)
                if kind not in {"channel", "bot", "private", "saved"}:
                    continue
                chat_id = str(message.chat_id or "")
                item = self._message_to_track(message, chat_id)
                if not item:
                    continue
                existing = known.get(chat_id, {})
                track_sources[chat_id] = {
                    "chatId": chat_id,
                    "kind": kind,
                    "title": utils.get_display_name(entity) or "Untitled chat",
                    "username": getattr(entity, "username", None),
                    "selected": bool(existing.get("selected")),
                    "lastPostAt": int(message.date.timestamp()) if message.date else None,
                }
                tracks.append(item)
                if len(tracks) >= limit:
                    break
            for source in track_sources.values():
                self.database.upsert_source(source, True)
            self.database.upsert_tracks(tracks)
            summaries = self.database.track_summaries(
                [track_key(item["chatId"], item["messageId"]) for item in tracks]
            )
            needle = normalize_text(cleaned)
            source_matches: list[dict[str, Any]] = []
            async for dialog in client.iter_dialogs():
                kind = self.classify_entity(dialog.entity)
                title = dialog.name or "Untitled chat"
                username = getattr(dialog.entity, "username", None)
                if not kind or (
                    needle not in normalize_text(title)
                    and needle not in normalize_text(username)
                ):
                    continue
                existing = known.get(str(dialog.id), {})
                source = {
                    "chatId": str(dialog.id),
                    "kind": kind,
                    "title": title,
                    "username": username,
                    "selected": bool(existing.get("selected")),
                    "trackCount": int(existing.get("trackCount", 0)),
                    "avatarUrl": f"/api/sources/{dialog.id}/avatar",
                }
                self.database.upsert_source(source, True)
                source_matches.append(source)
                if len(source_matches) >= 10:
                    break
            return {"tracks": summaries, "sources": source_matches}

    def start_preview(self, chat_id: str) -> dict[str, Any]:
        source = self.database.get_source(chat_id)
        if not source:
            raise KeyError("Source not found")
        if active := self.jobs.active("preview", chat_id):
            return active.public()
        job = BackgroundJob(secrets.token_urlsafe(12), "preview", chat_id=chat_id, mode="full")
        return self.jobs.start(job, self._run_preview(job), error_mapper=self._friendly_sync_error)

    async def _run_preview(self, job: BackgroundJob) -> None:
        # Only scan the whole history the first time. Once a preview has completed,
        # lastMessageId is set and an incremental pass picks up just the new messages,
        # so returning to a temporary source is fast instead of a full rescan.
        source = self.database.get_source(job.chat_id)
        first_visit = not int((source or {}).get("lastMessageId") or 0)
        await self.sync_source(job.chat_id, full=first_visit, job=job, temporary=True)

    async def add_source(self, chat_id: str) -> dict[str, Any]:
        source = next(
            (item for item in await self.discover_sources() if item["chatId"] == chat_id),
            None,
        )
        if not source:
            raise KeyError("Eligible Telegram chat not found")
        source["selected"] = True
        self.database.upsert_source(source)
        self._refresh_watched_ids()
        self.discovery_cache = None
        job = self.start_sync(chat_id, full=True)
        return {"source": self.database.get_source(chat_id) or source, "job": job}

    async def set_source_selected(self, chat_id: str, selected: bool) -> dict[str, Any]:
        source = self.database.get_source(chat_id)
        if selected and not source:
            return await self.add_source(chat_id)
        if not source:
            raise KeyError("Source not found")
        self.database.set_source_selected(chat_id, selected)
        self._refresh_watched_ids()
        self.discovery_cache = None
        if not selected:
            self.jobs.cancel("sync", chat_id)
            self.jobs.cancel("preview", chat_id)
            return {"source": self.database.get_source(chat_id), "job": None}
        return {"source": self.database.get_source(chat_id), "job": self.start_sync(chat_id, True)}

    async def sync_all(self) -> None:
        for source in self.database.list_sources():
            self.start_sync(source["chatId"])

    def start_sync(self, chat_id: str, full: bool = False) -> dict[str, Any]:
        source = self.database.get_source(chat_id)
        if not source or not source["selected"]:
            raise KeyError("Selected source not found")
        if active := self.jobs.active("sync", chat_id):
            return active.public()
        job = BackgroundJob(
            secrets.token_urlsafe(12), "sync", chat_id=chat_id, mode="full" if full else "incremental"
        )
        return self.jobs.start(job, self._run_sync(job, full), error_mapper=self._friendly_sync_error)

    async def _run_sync(self, job: BackgroundJob, full: bool) -> None:
        await self.sync_source(job.chat_id, full=full, job=job)
        # New tracks just landed; feed the cover-enrichment job the oldest ones first.
        self.maybe_enrich()

    async def sync_source(
        self, chat_id: str, full: bool = False, job: BackgroundJob | None = None,
        temporary: bool = False,
    ) -> dict[str, Any]:
        client = self.require_client()
        source = self.database.get_source(chat_id)
        if not source:
            raise KeyError("Source not found")
        lock = self.sync_locks.get(chat_id)
        if lock is None:
            lock = self.sync_locks[chat_id] = asyncio.Lock()
        # Bound before the try so the cancellation handler can still flush partial progress
        # if we are cancelled during get_entity, before the scan loop assigns them.
        minimum = 0 if full else int(source["lastMessageId"] or 0)
        highest_scanned = minimum
        seen: set[str] = set()
        items: dict[str, dict[str, Any]] = {}
        # Bound before the try like minimum/highest_scanned, so a cancel during get_entity
        # (before the scan loop assigns it) cannot NameError in the cancellation handler.
        generation: int | None = None
        async with self.sync_semaphore:
            async with lock:
                try:
                    if job:
                        job.state = "running"
                    entity = await asyncio.wait_for(client.get_entity(int(chat_id)), timeout=30)
                    # A full scan owns the availability decision: tracks it never re-sees
                    # become unavailable once it completes (complete_sync_generation).
                    # Incremental and preview scans never mark anything, so they open no
                    # generation -- a failed or interrupted scan must never flip rows.
                    generation = (
                        self.database.begin_sync_generation(chat_id) if full and not temporary else None
                    )
                    # Single-pass scan: one iter_messages over the history, filtering
                    # audio/document in Python. Halves API round-trips vs the old
                    # two-pass (Music + Document). Kept simple — no filter= arg so
                    # Telegram doesn't skip messages the client-side check would keep.
                    async for message in client.iter_messages(entity, min_id=minimum):
                        highest_scanned = max(highest_scanned, int(message.id))
                        if job:
                            job.processed += 1
                        item = self._message_to_track(message, chat_id)
                        if not item:
                            continue
                        seen.add(str(message.id))
                        items[str(message.id)] = item
                        if job:
                            job.found = len(seen)
                        if len(items) >= 100:
                            await asyncio.to_thread(
                                self.database.upsert_tracks, list(items.values()), seen_generation=generation
                            )
                            items.clear()
                    await asyncio.to_thread(
                        self.database.upsert_tracks, list(items.values()), seen_generation=generation
                    )
                    if generation is not None:
                        self.database.complete_sync_generation(chat_id, generation)
                    highest = max(highest_scanned, minimum)
                    self.database.finish_sync(chat_id, highest)
                    return self.database.get_source(chat_id) or source
                except asyncio.CancelledError:
                    # Keep the tracks we already read, but do NOT advance lastMessageId.
                    # iter_messages walks newest to oldest, so highest_scanned is the newest
                    # id after the very first message; persisting it here would make the next
                    # incremental sync skip every older message we never got to.
                    self.database.upsert_tracks(list(items.values()), seen_generation=generation)
                    raise
                except Exception as error:
                    self.database.finish_sync(
                        chat_id, int(source["lastMessageId"] or 0), self._friendly_sync_error(error)
                    )
                    raise

    async def _forward_ranking(self) -> dict[str, float]:
        """Map contact id -> Telegram's own "who you forward to" rating, best first.

        Uses the forward_users category rather than correspondents: this dialog forwards a
        track, and the two orderings really do differ (the account this was built against has
        a #1 correspondent who is only its #2 forward target). Ratings decay over time, so
        Telegram's numbers already reflect recency as well as volume.

        Never raises. Top peers are a nicety, and the whole picker would be unusable if a
        disabled-suggestions setting or a transient RPC error took it down.
        """
        try:
            result = await self.require_client()(functions.contacts.GetTopPeersRequest(
                offset=0, limit=TOP_PEER_LIMIT, hash=0, forward_users=True,
            ))
        except Exception:
            # Includes TopPeersDisabled surfacing as an error, flood waits and offline blips.
            return {}
        if not isinstance(result, contacts_types.TopPeers):
            # TopPeersDisabled: the user turned off "suggest frequent contacts" in Telegram.
            # Respect that and show the plain alphabetical list.
            return {}
        ranking: dict[str, float] = {}
        for category in result.categories:
            for peer in category.peers:
                user_id = getattr(peer.peer, "user_id", None)
                if user_id is not None:
                    ranking[str(user_id)] = peer.rating
        return ranking

    async def contacts(self) -> list[dict[str, Any]]:
        result = await self.require_client()(functions.contacts.GetContactsRequest(hash=0))
        ranking = await self._forward_ranking()
        contacts = []
        for user in result.users:
            if getattr(user, "deleted", False) or getattr(user, "bot", False) or getattr(user, "is_self", False):
                continue
            user_id = str(user.id)
            contacts.append({
                "id": user_id,
                "name": utils.get_display_name(user) or "Unnamed contact",
                "username": getattr(user, "username", None),
                "avatarUrl": f"/api/sources/{user.id}/avatar",
                # Intersected with real contacts on purpose: top peers include bots and people
                # who were never added, but forward_track() only accepts ids from this list, so
                # ranking stays an ordering hint and never widens who can receive a track.
                "forwardRank": ranking.get(user_id),
            })
        return sorted(contacts, key=lambda item: item["name"].casefold())

    async def forward_track(self, track: dict[str, Any], recipient_id: str | None = None) -> dict[str, Any]:
        client = self.require_client()
        destination: Any = "me"
        if recipient_id is not None:
            allowed = {item["id"] for item in await self.contacts()}
            if recipient_id not in allowed:
                raise ValueError("Choose a Telegram contact from the list")
            destination = int(recipient_id)
        messages = await client.forward_messages(
            destination,
            int(track["messageId"]),
            from_peer=int(track["chatId"]),
        )
        if not messages:
            raise RuntimeError("Telegram did not forward this track")
        if not isinstance(messages, list):
            messages = [messages]
        return {"ok": True, "messageId": str(messages[0].id)}

    @staticmethod
    def _friendly_sync_error(error: Exception) -> str:
        if isinstance(error, FloodWaitError):
            return f"Retry after {error.seconds} seconds"
        if isinstance(error, asyncio.TimeoutError):
            return "Telegram did not respond in time; check the connection and retry"
        if isinstance(error, RPCError):
            return "Telegram rejected this sync; open the chat in Telegram and retry"
        return "Sync failed; check the Telegram connection and retry"

    @staticmethod
    def _message_to_track(message: Any, chat_id: str) -> dict[str, Any] | None:
        document = getattr(message, "document", None)
        if not document:
            return None
        file_name = ""
        title = ""
        artist = ""
        duration = 0
        is_voice = False
        for attribute in document.attributes:
            if isinstance(attribute, DocumentAttributeFilename):
                file_name = attribute.file_name
            elif isinstance(attribute, DocumentAttributeAudio):
                is_voice = bool(attribute.voice)
                title = attribute.title or ""
                artist = attribute.performer or ""
                duration = int(attribute.duration or 0)
        if is_voice or not is_audio_file(file_name, document.mime_type):
            return None
        if not file_name:
            file_name = f"telegram-{message.id}{utils.get_extension(document) or '.audio'}"
        if not title:
            title = Path(file_name).stem or "Unknown title"
        return {
            "chatId": chat_id,
            "messageId": str(message.id),
            "fileName": file_name,
            "mimeType": document.mime_type or "application/octet-stream",
            "fileSize": int(document.size or 0),
            "durationMs": duration * 1000,
            "title": title,
            "artist": artist or "Unknown artist",
            "sentAt": int(message.date.timestamp()) if message.date else 0,
            "documentId": str(document.id),
        }

    async def thumbnail(self, chat_id: str, message_id: str, quality: str = "default") -> bytes | None:
        client = self.require_client()
        track = self.database.get_track(chat_id, message_id)
        if not track or not track["available"]:
            raise KeyError("Track is unavailable")
        key_digest = hashlib.sha256(track["key"].encode()).hexdigest()[:20]
        quality_tag = "hi" if quality == "high" else "lo"
        fingerprint = f"{media_identity(track.get('documentId'), track['file']['size'])}:{quality}"
        version = hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
        destination = self.thumbnail_directory / f"{key_digest}-{quality_tag}-{version}.jpg"
        missing = self.thumbnail_directory / f"{key_digest}-{quality_tag}-{version}.missing"
        if destination.is_file():
            return destination.read_bytes()
        if missing.is_file() and time.time() - missing.stat().st_mtime < 24 * 60 * 60:
            return None
        for stale in self.thumbnail_directory.glob(f"{key_digest}-{quality_tag}-*"):
            if stale not in {destination, missing}:
                stale.unlink(missing_ok=True)
        message, document = await self.media.get_message_document(chat_id, message_id)
        if not getattr(document, "thumbs", None):
            missing.touch()
            return None
        if quality == "high":
            sizes = [(t.type, t.w * t.h if hasattr(t, "w") and t.w else 0, t) for t in document.thumbs]
            sizes.sort(key=lambda s: s[1], reverse=True)
            thumb_type = sizes[0][0] if sizes else -1
        else:
            thumb_type = -1
        result = await asyncio.wait_for(client.download_media(message, thumb=thumb_type, file=bytes), timeout=30)
        if not result:
            missing.touch()
            return None
        data = bytes(result)
        destination.write_bytes(data)
        os.chmod(destination, 0o600)
        missing.unlink(missing_ok=True)
        return data

    async def avatar(self, chat_id: str) -> bytes | None:
        client = self.require_client()
        digest = hashlib.sha256(chat_id.encode()).hexdigest()
        destination = self.avatar_directory / f"{digest}.jpg"
        missing = self.avatar_directory / f"{digest}.missing"
        if destination.is_file():
            return destination.read_bytes()
        if missing.is_file() and time.time() - missing.stat().st_mtime < 24 * 60 * 60:
            return None
        entity = await client.get_entity(int(chat_id))
        result = await client.download_profile_photo(entity, file=bytes, download_big=False)
        if not result:
            missing.touch()
            return None
        data = bytes(result)
        destination.write_bytes(data)
        os.chmod(destination, 0o600)
        missing.unlink(missing_ok=True)
        return data

    def start_source_counts(self, sources: list[dict[str, Any]]) -> dict[str, Any]:
        if active := self.jobs.active("source-counts"):
            return active.public()
        job = BackgroundJob(secrets.token_urlsafe(12), "source-counts")
        return self.jobs.start(job, self._run_source_counts(job, sources), error_mapper=self._friendly_sync_error)

    async def _run_source_counts(self, job: BackgroundJob, sources: list[dict[str, Any]]) -> None:
        job.result = {}
        client = self.require_client()
        for source in sources:
            chat_id = source["chatId"]
            cached = self.database.cache_get(f"source-count:{chat_id}")
            if cached is None:
                entity = await client.get_entity(int(chat_id))
                result = await client.get_messages(
                    entity, limit=0, filter=InputMessagesFilterMusic()
                )
                cached = int(getattr(result, "total", 0))
                self.database.cache_set(f"source-count:{chat_id}", cached, 600)
            job.result[chat_id] = int(cached)
            job.processed += 1

    def maybe_enrich(self) -> None:
        """Start the auto cover-art job if the setting is on and a worker is wired.

        Runs after a successful source sync and at startup; both are cheap no-ops when
        enrichment is disabled or no MusicBrainz contact is configured.
        """
        if not self.enrich_worker:
            return
        try:
            settings = self.database.get_settings()
        except Exception:
            return
        if not settings.get("autoArtwork", True):
            return
        self.start_enrich(manual=False)

    def start_enrich(self, manual: bool = False) -> dict[str, Any]:
        if active := self.jobs.active("enrich"):
            return active.public()
        job = BackgroundJob(secrets.token_urlsafe(12), "enrich")
        job.result = {"added": 0, "missed": 0, "skipped": None}
        if not self.enrich_worker:
            job.state = "complete"
            return self.jobs.register(job)
        return self.jobs.start(job, self._run_enrich(job, manual), error_mapper=self._friendly_enrich_error)

    async def _run_enrich(self, job: BackgroundJob, manual: bool) -> None:
        result = await self.enrich_worker(manual)
        job.result = result
        job.found = int(result.get("added") or 0)
        job.processed = job.found + int(result.get("missed") or 0)

    @staticmethod
    def _friendly_enrich_error(error: Exception) -> str:
        if isinstance(error, httpx.HTTPStatusError):
            return "Cover art service rejected the request; try again shortly"
        if isinstance(error, asyncio.TimeoutError):
            return "Cover art service did not respond in time; check the connection and retry"
        return "Cover enrichment failed; check the connection and retry"

    def start_prefetch(self, keys: list[str]) -> dict[str, Any]:
        count = int(self.database.get_settings()["prefetchCount"])
        selected = list(dict.fromkeys(keys))[:count]
        if active := self.jobs.active("prefetch"):
            if selected == self.prefetch_order:
                return active.public()
            # A different selection replaces the running prefetch; the runner owns the cancel.
            self.jobs.cancel("prefetch")
        self.prefetch_order = selected
        # MediaCache.protected_keys is this same set object, shared by reference so eviction
        # never deletes a track being fetched. Rebind it here and the protection strands on
        # the old set; mutate in place so the cache always sees the active selection.
        self.prefetch_keys.clear()
        self.prefetch_keys.update(selected)
        job = BackgroundJob(secrets.token_urlsafe(12), "prefetch", result={})
        if not selected:
            job.state = "complete"
            return self.jobs.register(job)
        return self.jobs.start(job, self._run_prefetch(job, selected), error_mapper=self._friendly_sync_error)

    async def _run_prefetch(self, job: BackgroundJob, keys: list[str]) -> None:
        async def _prefetch_one(key):
            chat_id, message_id = key.split(":", 1)
            track = self.database.get_track(chat_id, message_id)
            if not track or not track["available"]:
                return "unavailable"
            if self.media.cached_media(track):
                job.processed += 1
                return "ready"
            await self.media.cache_media(track)
            job.found += 1
            job.processed += 1
            return "ready"

        results = await asyncio.gather(
            *[_prefetch_one(key) for key in keys], return_exceptions=True
        )
        for i, result in enumerate(results):
            job.result[keys[i]] = "error" if isinstance(result, Exception) else result

    def cache_status(self, keys: list[str] | None = None) -> dict[str, Any]:
        entries = self.database.media_cache_entries()
        ready = {entry["track_key"] for entry in entries}
        wanted = keys or []
        prefetch = self.jobs.active("prefetch")
        active_result = prefetch.result if prefetch and prefetch.result else {}
        states = {
            key: "ready" if key in ready else active_result.get(key, "queued")
            for key in wanted
        }
        return {
            "bytes": sum(int(entry["size"]) for entry in entries),
            "files": len(entries),
            "states": states,
        }

    def clear_media_cache(self) -> dict[str, int]:
        # Cancelling the prefetch is a job-lifecycle concern, so it stays here; the file and
        # database deletion belongs to the media cache.
        self.jobs.cancel("prefetch")
        self.prefetch_keys.clear()
        self.prefetch_order.clear()
        return self.media.clear_cache()
