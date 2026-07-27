from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import segno
from cryptography.fernet import Fernet, InvalidToken
from telethon import TelegramClient, events, functions, types, utils
from telethon.errors import FloodWaitError, RPCError, SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    InputMessagesFilterDocument,
    InputMessagesFilterMusic,
)

from core import Database, is_audio_file, normalize_text, track_key


FLOW_TTL_SECONDS = 300
MEDIA_CHUNK_SIZE = 512 * 1024
PARTIAL_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60


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


@dataclass
class BackgroundJob:
    id: str
    kind: str
    chat_id: str = ""
    mode: str = ""
    state: str = "queued"
    processed: int = 0
    found: int = 0
    error: str = ""
    result: Any = None
    created_at: float = field(default_factory=time.monotonic)
    task: asyncio.Task[Any] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "jobId": self.id,
            "kind": self.kind,
            "chatId": self.chat_id or None,
            "mode": self.mode or None,
            "state": self.state,
            "processed": self.processed,
            "found": self.found,
            "error": self.error or None,
            "result": self.result,
        }


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
        self.jobs: dict[str, BackgroundJob] = {}
        # ponytail: per-source locks beat one global lock; same chat_id can't double-sync, different ones run in parallel.
        self.sync_locks: dict[str, asyncio.Lock] = {}
        # ponytail: caps concurrent scans to avoid FloodWait; bump if profiling shows idle headroom.
        self.sync_semaphore = asyncio.Semaphore(3)
        self.global_search_lock = asyncio.Lock()
        # ponytail: one global transfer gate is enough for one owner; split by DC only if profiling proves it.
        self.media_semaphore = asyncio.Semaphore(4)
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self.avatar_directory = data_directory / "avatars"
        self.thumbnail_directory = data_directory / "thumbnails"
        self.media_directory = data_directory / "media-cache"
        self.download_directory = data_directory / "tagged-downloads"
        self.avatar_directory.mkdir(parents=True, exist_ok=True)
        self.thumbnail_directory.mkdir(parents=True, exist_ok=True)
        self.media_directory.mkdir(parents=True, exist_ok=True)
        self.download_directory.mkdir(parents=True, exist_ok=True)
        self.cache_lock = asyncio.Lock()
        self.prefetch_job: BackgroundJob | None = None
        self.prefetch_keys: set[str] = set()
        self.prefetch_order: list[str] = []
        self.document_cache: dict[str, tuple[float, str, Any, Any]] = {}
        self.discovery_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._countries: list[dict[str, str]] | None = None
        self._countries_updated: float = 0
        self._clean_partial_cache()

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
        self._install_handlers(client)
        self._spawn(self.sync_all())

    async def stop(self) -> None:
        for task in list(self._background_tasks):
            task.cancel()
        await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        for flow in list(self.flows.values()):
            if flow.task:
                flow.task.cancel()
            await flow.client.disconnect()
        self.flows.clear()
        if self.client:
            await self.client.disconnect()
            self.client = None

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _clean_partial_cache(self) -> None:
        cutoff = time.time() - PARTIAL_CACHE_TTL_SECONDS
        for candidate in self.media_directory.glob("*.part"):
            try:
                details = candidate.stat()
                if not details.st_size or details.st_mtime < cutoff:
                    candidate.unlink(missing_ok=True)
            except OSError:
                candidate.unlink(missing_ok=True)

    def _start_job(self, job: BackgroundJob, coroutine: Any) -> dict[str, Any]:
        self._prune_jobs()
        self.jobs[job.id] = job
        task = asyncio.create_task(coroutine)
        job.task = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return job.public()

    def job_status(self, job_id: str) -> dict[str, Any]:
        self._prune_jobs()
        job = self.jobs.get(job_id)
        if not job:
            raise KeyError("Background job not found")
        return job.public()

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if not job:
            raise KeyError("Background job not found")
        if job.state in {"queued", "running"} and job.task:
            job.task.cancel()
        return job.public()

    def _prune_jobs(self) -> None:
        now = time.monotonic()
        terminal = [
            job for job in self.jobs.values()
            if job.state not in {"queued", "running"}
        ]
        remove = {job.id for job in terminal if now - job.created_at > 15 * 60}
        survivors = sorted(
            (job for job in terminal if job.id not in remove),
            key=lambda job: job.created_at,
            reverse=True,
        )
        remove.update(job.id for job in survivors[100:])
        for job_id in remove:
            self.jobs.pop(job_id, None)

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

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        await self._save_event_message(event)

    async def _on_edited_message(self, event: events.MessageEdited.Event) -> None:
        await self._save_event_message(event)

    async def _save_event_message(self, event: Any) -> None:
        chat_id = str(event.chat_id or "")
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
        code = segno.make(qr.url)
        svg = code.svg_inline(scale=5, border=0, dark="#111111", light="#ffffff")
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
        self._install_handlers(flow.client)
        flow.error = ""
        flow.state = "ready"
        self._spawn(self.sync_all())

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
        async for dialog in client.iter_dialogs():
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
        active = next((
            job for job in self.jobs.values()
            if job.kind == "preview" and job.chat_id == chat_id
            and job.state in {"queued", "running"}
        ), None)
        if active:
            return active.public()
        job = BackgroundJob(secrets.token_urlsafe(12), "preview", chat_id=chat_id, mode="full")
        return self._start_job(job, self._run_preview(job))

    async def _run_preview(self, job: BackgroundJob) -> None:
        try:
            await self.sync_source(job.chat_id, full=True, job=job, temporary=True)
            job.state = "complete"
        except asyncio.CancelledError:
            job.state = "cancelled"
        except Exception as error:
            job.error = self._friendly_sync_error(error)
            job.state = "error"

    async def add_source(self, chat_id: str) -> dict[str, Any]:
        source = next(
            (item for item in await self.discover_sources() if item["chatId"] == chat_id),
            None,
        )
        if not source:
            raise KeyError("Eligible Telegram chat not found")
        source["selected"] = True
        self.database.upsert_source(source)
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
        self.discovery_cache = None
        if not selected:
            for job in self.jobs.values():
                if job.chat_id == chat_id and job.state in {"queued", "running"} and job.task:
                    job.task.cancel()
            return {"source": self.database.get_source(chat_id), "job": None}
        return {"source": self.database.get_source(chat_id), "job": self.start_sync(chat_id, True)}

    async def sync_all(self) -> None:
        for source in self.database.list_sources():
            self.start_sync(source["chatId"])

    def start_sync(self, chat_id: str, full: bool = False) -> dict[str, Any]:
        source = self.database.get_source(chat_id)
        if not source or not source["selected"]:
            raise KeyError("Selected source not found")
        active = next(
            (
                job for job in self.jobs.values()
                if job.kind == "sync" and job.chat_id == chat_id and job.state in {"queued", "running"}
            ),
            None,
        )
        if active:
            return active.public()
        job = BackgroundJob(
            secrets.token_urlsafe(12), "sync", chat_id=chat_id, mode="full" if full else "incremental"
        )
        return self._start_job(job, self._run_sync(job, full))

    async def _run_sync(self, job: BackgroundJob, full: bool) -> None:
        try:
            await self.sync_source(job.chat_id, full=full, job=job)
            job.state = "complete"
        except asyncio.CancelledError:
            job.state = "cancelled"
        except Exception as error:
            job.error = self._friendly_sync_error(error)
            job.state = "error"

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
        async with self.sync_semaphore:
            async with lock:
                try:
                    if job:
                        job.state = "running"
                    entity = await client.get_entity(int(chat_id))
                    minimum = 0 if full else int(source["lastMessageId"] or 0)
                    highest_scanned = minimum
                    seen: set[str] = set()
                    items: dict[str, dict[str, Any]] = {}
                    for filter_type in (InputMessagesFilterMusic, InputMessagesFilterDocument):
                        async for message in client.iter_messages(
                            entity, filter=filter_type(), min_id=minimum
                        ):
                            highest_scanned = max(highest_scanned, int(message.id))
                            if job:
                                job.processed += 1
                            item = self._message_to_track(message, chat_id)
                            if item:
                                seen.add(str(message.id))
                                items[str(message.id)] = item
                                if job:
                                    job.found = len(seen)
                                if len(items) >= 100:
                                    self.database.upsert_tracks(list(items.values()))
                                    items.clear()
                    self.database.upsert_tracks(list(items.values()))
                    if full and not temporary:
                        self.database.mark_missing_unavailable(chat_id, seen)
                    highest = max(highest_scanned, minimum)
                    self.database.finish_sync(chat_id, highest)
                    return self.database.get_source(chat_id) or source
                except Exception as error:
                    self.database.finish_sync(
                        chat_id, int(source["lastMessageId"] or 0), self._friendly_sync_error(error)
                    )
                    raise

    async def contacts(self) -> list[dict[str, Any]]:
        result = await self.require_client()(functions.contacts.GetContactsRequest(hash=0))
        contacts = []
        for user in result.users:
            if getattr(user, "deleted", False) or getattr(user, "bot", False) or getattr(user, "is_self", False):
                continue
            contacts.append({
                "id": str(user.id),
                "name": utils.get_display_name(user) or "Unnamed contact",
                "username": getattr(user, "username", None),
                "avatarUrl": f"/api/sources/{user.id}/avatar",
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

    async def get_message_document(self, chat_id: str, message_id: str) -> tuple[Any, Any]:
        client = self.require_client()
        track = self.database.get_track(chat_id, message_id)
        if not track or not track["available"]:
            raise KeyError("Track is unavailable")
        key = track_key(chat_id, message_id)
        fingerprint = f"{track.get('documentId')}:{track['file']['size']}"
        cached = self.document_cache.get(key)
        if cached and cached[0] > time.monotonic() and cached[1] == fingerprint:
            self.document_cache.pop(key)
            self.document_cache[key] = cached
            return cached[2], cached[3]
        message = await client.get_messages(int(chat_id), ids=int(message_id))
        document = getattr(message, "document", None) if message else None
        if not document:
            self.document_cache.pop(key, None)
            self.database.mark_unavailable(chat_id, [message_id])
            raise KeyError("Telegram media is no longer available")
        self.document_cache[key] = (time.monotonic() + 600, fingerprint, message, document)
        while len(self.document_cache) > 128:
            self.document_cache.pop(next(iter(self.document_cache)))
        return message, document

    async def iter_media(
        self, document: Any, start: int, length: int
    ) -> AsyncIterator[bytes]:
        client = self.require_client()
        request_size = MEDIA_CHUNK_SIZE
        chunks = math.ceil(length / request_size)
        remaining = length
        async with self.media_semaphore:
            iterator = client.iter_download(
                document,
                offset=start,
                limit=chunks,
                chunk_size=request_size,
                request_size=request_size,
                file_size=int(document.size or 0),
            )
            try:
                async for chunk in iterator:
                    if remaining <= 0:
                        break
                    data = bytes(chunk[:remaining])
                    remaining -= len(data)
                    if data:
                        yield data
            finally:
                close = getattr(iterator, "close", None)
                if close:
                    await close()

    async def thumbnail(self, chat_id: str, message_id: str, quality: str = "default") -> bytes | None:
        client = self.require_client()
        track = self.database.get_track(chat_id, message_id)
        if not track or not track["available"]:
            raise KeyError("Track is unavailable")
        key_digest = hashlib.sha256(track["key"].encode()).hexdigest()[:20]
        quality_tag = "hi" if quality == "high" else "lo"
        fingerprint = f"{track.get('documentId')}:{track['file']['size']}:{quality}"
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
        message, document = await self.get_message_document(chat_id, message_id)
        if not getattr(document, "thumbs", None):
            missing.touch()
            return None
        if quality == "high":
            sizes = [(t.type, t.w * t.h if hasattr(t, "w") and t.w else 0, t) for t in document.thumbs]
            sizes.sort(key=lambda s: s[1], reverse=True)
            thumb_type = sizes[0][0] if sizes else -1
        else:
            thumb_type = -1
        result = await client.download_media(message, thumb=thumb_type, file=bytes)
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
        active = next(
            (job for job in self.jobs.values() if job.kind == "source-counts" and job.state in {"queued", "running"}),
            None,
        )
        if active:
            return active.public()
        job = BackgroundJob(secrets.token_urlsafe(12), "source-counts")
        return self._start_job(job, self._run_source_counts(job, sources))

    async def _run_source_counts(self, job: BackgroundJob, sources: list[dict[str, Any]]) -> None:
        job.state = "running"
        job.result = {}
        try:
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
            job.state = "complete"
        except asyncio.CancelledError:
            job.state = "cancelled"
        except Exception as error:
            job.error = self._friendly_sync_error(error)
            job.state = "error"

    def _cache_path(self, name: str) -> Path | None:
        candidate = (self.media_directory / Path(name).name).resolve()
        return candidate if candidate.parent == self.media_directory.resolve() else None

    def cached_media(self, track: dict[str, Any]) -> Path | None:
        fingerprint = (
            f"{track.get('documentId')}:{track['file']['size']}" if track.get("documentId") else ""
        )
        entry = self.database.get_media_cache(track["key"], fingerprint)
        if not entry or not (candidate := self._cache_path(entry["path"])) or not candidate.is_file():
            return None
        if candidate.stat().st_size != int(track["file"]["size"]):
            self.database.delete_media_cache([track["key"]])
            candidate.unlink(missing_ok=True)
            return None
        return candidate

    async def cache_media(self, track: dict[str, Any]) -> Path:
        if cached := self.cached_media(track):
            return cached
        async with self.cache_lock:
            if cached := self.cached_media(track):
                return cached
            _, document = await self.get_message_document(track["chatId"], track["messageId"])
            expected_size = int(document.size or 0)
            if expected_size <= 0:
                raise RuntimeError("Telegram reported an empty media file")
            fingerprint = f"{document.id}:{expected_size}"
            digest = hashlib.sha256(f"{track['key']}:{fingerprint}".encode()).hexdigest()
            temporary = self.media_directory / f"{digest}.part"
            destination = self.media_directory / f"{digest}.audio"
            stale_entry = self.database.get_media_cache(track["key"])
            if destination.is_file() and destination.stat().st_size == expected_size:
                os.chmod(destination, 0o600)
                if stale_entry and stale_entry["path"] != destination.name:
                    if stale := self._cache_path(stale_entry["path"]):
                        stale.unlink(missing_ok=True)
                self.database.save_media_cache(track["key"], fingerprint, destination.name, expected_size)
                return destination
            destination.unlink(missing_ok=True)
            offset = temporary.stat().st_size if temporary.is_file() else 0
            if offset > expected_size:
                temporary.unlink(missing_ok=True)
                offset = 0
            elif offset < expected_size and offset % MEDIA_CHUNK_SIZE:
                offset -= offset % MEDIA_CHUNK_SIZE
                with temporary.open("r+b") as output:
                    output.truncate(offset)
            if offset < expected_size:
                with temporary.open("ab") as output:
                    os.chmod(temporary, 0o600)
                    async for chunk in self.iter_media(document, offset, expected_size - offset):
                        output.write(chunk)
            if temporary.stat().st_size != expected_size:
                raise RuntimeError("Telegram media download ended before the file was complete")
            temporary.replace(destination)
            os.chmod(destination, 0o600)
            if stale_entry and stale_entry["path"] != destination.name:
                if stale := self._cache_path(stale_entry["path"]):
                    stale.unlink(missing_ok=True)
            self.database.save_media_cache(
                track["key"], fingerprint, destination.name, destination.stat().st_size
            )
            await self._evict_cache()
            return destination

    async def tagged_download(self, track: dict[str, Any]) -> Path | None:
        if not track.get("overrides"):
            return None
        source = await self.cache_media(track)
        metadata = track["metadata"]
        fingerprint = json.dumps(
            [track["key"], track.get("documentId"), track["file"]["size"], track["overrides"]],
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(fingerprint.encode()).hexdigest()
        suffix = Path(track["file"]["name"]).suffix or ".mp3"
        destination = self.download_directory / f"{digest}{suffix}"
        if destination.is_file():
            return destination
        temporary = self.download_directory / f"{digest}.part{suffix}"
        fields = {
            "title": metadata.get("title"),
            "artist": metadata.get("artist"),
            "album": metadata.get("album"),
            "album_artist": metadata.get("albumArtist"),
            "genre": metadata.get("genre"),
            "date": metadata.get("year"),
            "track": metadata.get("trackNumber"),
            "disc": metadata.get("discNumber"),
        }
        command = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(source), "-map", "0", "-c", "copy"]
        for name, value in fields.items():
            if value not in (None, "", 0):
                command.extend(["-metadata", f"{name}={value}"])
        command.append(str(temporary))
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
        except OSError:
            return None
        _, stderr = await process.communicate()
        if process.returncode:
            temporary.unlink(missing_ok=True)
            return None
        temporary.replace(destination)
        os.chmod(destination, 0o600)
        return destination

    def start_prefetch(self, keys: list[str]) -> dict[str, Any]:
        count = int(self.database.get_settings()["prefetchCount"])
        selected = list(dict.fromkeys(keys))[:count]
        if (
            selected == self.prefetch_order
            and self.prefetch_job
            and self.prefetch_job.state in {"queued", "running"}
        ):
            return self.prefetch_job.public()
        self.prefetch_order = selected
        self.prefetch_keys = set(selected)
        if self.prefetch_job and self.prefetch_job.state in {"queued", "running"} and self.prefetch_job.task:
            self.prefetch_job.task.cancel()
        job = BackgroundJob(secrets.token_urlsafe(12), "prefetch", result={})
        self.prefetch_job = job
        if not selected:
            job.state = "complete"
            self._prune_jobs()
            self.jobs[job.id] = job
            return job.public()
        return self._start_job(job, self._run_prefetch(job, selected))

    async def _run_prefetch(self, job: BackgroundJob, keys: list[str]) -> None:
        job.state = "running"
        try:
            async def _prefetch_one(key):
                chat_id, message_id = key.split(":", 1)
                track = self.database.get_track(chat_id, message_id)
                if not track or not track["available"]:
                    return "unavailable"
                if self.cached_media(track):
                    job.processed += 1
                    return "ready"
                await self.cache_media(track)
                job.found += 1
                job.processed += 1
                return "ready"

            results = await asyncio.gather(
                *[_prefetch_one(key) for key in keys], return_exceptions=True
            )
            for i, result in enumerate(results):
                job.result[keys[i]] = "error" if isinstance(result, Exception) else result
            job.state = "complete"
        except asyncio.CancelledError:
            job.state = "cancelled"
        except Exception as error:
            job.error = self._friendly_sync_error(error)
            job.state = "error"

    async def _evict_cache(self, maximum: int = 5 * 1024 * 1024 * 1024) -> None:
        await asyncio.to_thread(self._evict_cache_sync, maximum)

    def _evict_cache_sync(self, maximum: int = 5 * 1024 * 1024 * 1024) -> None:
        entries = self.database.media_cache_entries()
        total = sum(int(entry["size"]) for entry in entries)
        remove: list[str] = []
        for entry in entries:
            if total <= maximum:
                break
            if entry["track_key"] in self.prefetch_keys:
                continue
            total -= int(entry["size"])
            remove.append(entry["track_key"])
            if candidate := self._cache_path(entry["path"]):
                candidate.unlink(missing_ok=True)
        if remove:
            self.database.delete_media_cache(remove)

    def cache_status(self, keys: list[str] | None = None) -> dict[str, Any]:
        entries = self.database.media_cache_entries()
        ready = {entry["track_key"] for entry in entries}
        wanted = keys or []
        active_result = self.prefetch_job.result if self.prefetch_job and self.prefetch_job.result else {}
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
        if self.prefetch_job and self.prefetch_job.task:
            self.prefetch_job.task.cancel()
        self.prefetch_job = None
        self.prefetch_keys.clear()
        self.prefetch_order.clear()
        paths = self.database.delete_media_cache()
        removed = 0
        for name in paths:
            if candidate := self._cache_path(name):
                if candidate.exists():
                    removed += candidate.stat().st_size
                    candidate.unlink()
        for candidate in self.media_directory.glob("*.part"):
            try:
                size = candidate.stat().st_size
                candidate.unlink()
                removed += size
            except OSError:
                candidate.unlink(missing_ok=True)
        return {"removedBytes": removed}
