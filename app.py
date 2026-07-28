from __future__ import annotations

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from telethon.errors import RPCError

from core import (
    BIND_HOSTS,
    SESSION_TTL_SECONDS,
    Database,
    RangeNotSatisfiable,
    parse_range_header,
    safe_filename,
    split_track_key,
    verify_password,
)
from external import ExternalServices
from telegram_service import TelegramService


ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)

# Embedded Telegram API credentials — app registration identifier, not a secret.
# Users authenticate with their own phone number; rate limits are per-account.
_DEFAULT_API_ID = 30986221
_DEFAULT_API_HASH = "7449770300bb823d8cc388103a973942"


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    encryption_key: str
    data_directory: Path
    musicbrainz_contact: str
    cookie_secure: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        env_api_id = os.environ.get("TELEGRAM_API_ID")
        api_id = int(env_api_id) if env_api_id else _DEFAULT_API_ID
        api_hash = os.environ.get("TELEGRAM_API_HASH", _DEFAULT_API_HASH)

        data_directory = Path(os.environ.get("DATA_DIR", ROOT / "data"))

        encryption_key = os.environ.get("APP_ENCRYPTION_KEY") or ""
        if not encryption_key:
            key_file = data_directory / "encryption.key"
            if key_file.is_file():
                encryption_key = key_file.read_text().strip()
            else:
                encryption_key = Fernet.generate_key().decode()
                data_directory.mkdir(parents=True, exist_ok=True)
                key_file.write_text(encryption_key)
                os.chmod(key_file, 0o600)

        try:
            Fernet(encryption_key.encode())
        except Exception as error:
            raise RuntimeError("APP_ENCRYPTION_KEY is not a valid Fernet key") from error

        return cls(
            api_id=api_id,
            api_hash=os.environ.get("TELEGRAM_API_HASH", _DEFAULT_API_HASH),
            encryption_key=encryption_key,
            data_directory=data_directory,
            musicbrainz_contact=os.environ.get("MUSICBRAINZ_CONTACT", ""),
            cookie_secure=os.environ.get("DEV_INSECURE_COOKIE") != "1",
        )


SESSION_COOKIE = "tt_session"

# Endpoints reachable without a session: the gate itself, the health probe, and the
# static shell that renders the login form.
PUBLIC_PATHS = {"/", "/healthz", "/sw.js", "/manifest.webmanifest", "/api/auth/status", "/api/auth/login"}


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class PasswordBody(BaseModel):
    current: str = Field(default="", max_length=1024)
    password: str = Field(min_length=8, max_length=1024)


class PasswordDisableBody(BaseModel):
    current: str = Field(min_length=1, max_length=1024)


class BindHostBody(BaseModel):
    bindHost: str = Field(pattern=r"^(127\.0\.0\.1|0\.0\.0\.0)$")


class PhoneBody(BaseModel):
    phone: str = Field(min_length=5, max_length=40)


class CodeBody(BaseModel):
    flowId: str = Field(min_length=10, max_length=200)
    code: str = Field(min_length=1, max_length=30)


class TelegramPasswordBody(BaseModel):
    flowId: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=1, max_length=1024)


class SourceBody(BaseModel):
    chatId: str = Field(pattern=r"^-?\d+$")


class SourceSelectionBody(BaseModel):
    selected: bool


class SourcePinBody(BaseModel):
    pinned: bool


class BulkSourcesBody(BaseModel):
    chatIds: list[str] = Field(max_length=10_000)
    selected: bool = False


class SourceOrderBody(BaseModel):
    chatIds: list[str] = Field(max_length=10_000)


class SyncBody(BaseModel):
    full: bool = False


class MetadataPatchBody(BaseModel):
    set: dict[str, Any] = Field(default_factory=dict)
    clear: list[str] = Field(default_factory=list)


class MetadataSearchBody(BaseModel):
    refresh: bool = False


class CandidateBody(BaseModel):
    candidateId: str = Field(min_length=1, max_length=200)
    fields: list[str] | None = None
    coverQuality: str | None = Field(default=None, pattern=r"^(500|1200|original)$")


class LyricsBody(BaseModel):
    text: str = Field(max_length=500_000)


class SettingsBody(BaseModel):
    musicbrainzContact: str | None = Field(default=None, max_length=300)
    coverQuality: str | None = Field(default=None, pattern=r"^(500|1200|original)$")
    prefetchCount: int | None = Field(default=None, ge=0, le=20)


class PlaybackEventBody(BaseModel):
    key: str
    event: str = Field(pattern=r"^(started|qualified|skipped)$")


class ShuffleBody(BaseModel):
    source: str | None = Field(default=None, pattern=r"^-?\d+$")
    currentKey: str = ""


class QueueBody(ShuffleBody):
    query: str = Field(default="", max_length=200)
    shuffle: bool = False
    liked: bool = False
    temporary: bool = False


class TrackKeysBody(BaseModel):
    keys: list[str] = Field(max_length=100)


class TelegramSearchBody(BaseModel):
    query: str = Field(min_length=3, max_length=200)
    limit: int = Field(default=30, ge=1, le=50)


class LikeBody(BaseModel):
    liked: bool


class ShareBody(BaseModel):
    recipientId: str = Field(pattern=r"^\d+$")


class PrefetchBody(BaseModel):
    keys: list[str] = Field(max_length=20)


# Login throttle: in-memory is correct here because sessions live in SQLite and a
# restart clearing the counter only costs an attacker the restart itself.
_LOGIN_WINDOW_SECONDS = 60
_LOGIN_MAX_ATTEMPTS = 5
_login_failures: dict[str, list[float]] = {}


def _login_allowed(client: str) -> bool:
    cutoff = time.monotonic() - _LOGIN_WINDOW_SECONDS
    recent = [stamp for stamp in _login_failures.get(client, []) if stamp > cutoff]
    if recent:
        _login_failures[client] = recent
    else:
        _login_failures.pop(client, None)
    return len(recent) < _LOGIN_MAX_ATTEMPTS


def _record_login_failure(client: str) -> None:
    _login_failures.setdefault(client, []).append(time.monotonic())
    if len(_login_failures) > 1000:
        cutoff = time.monotonic() - _LOGIN_WINDOW_SECONDS
        for key, stamps in list(_login_failures.items()):
            if not [stamp for stamp in stamps if stamp > cutoff]:
                _login_failures.pop(key, None)


def _clear_login_failures(client: str) -> None:
    _login_failures.pop(client, None)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        settings.data_directory.mkdir(parents=True, exist_ok=True)
        database = Database(settings.data_directory / "library.sqlite3")
        telegram = TelegramService(
            database,
            api_id=settings.api_id,
            api_hash=settings.api_hash,
            encryption_key=settings.encryption_key,
            data_directory=settings.data_directory,
        )
        external = ExternalServices(
            database, settings.data_directory / "artwork", settings.musicbrainz_contact
        )
        if settings.musicbrainz_contact and not database.get_settings()["musicbrainzContact"]:
            database.save_settings({"musicbrainzContact": settings.musicbrainz_contact})
        application.state.settings = settings
        application.state.database = database
        application.state.telegram = telegram
        application.state.external = external
        application.state.startup_error = None
        try:
            await telegram.start()
        except Exception:
            LOGGER.exception("Could not open the stored Telegram session")
            application.state.startup_error = "The stored Telegram session could not be opened"
        yield
        await external.close()
        await telegram.stop()
        database.close()

    application = FastAPI(title="Telegram Turntable", docs_url=None, redoc_url=None, lifespan=lifespan)
    application.add_middleware(GZipMiddleware, minimum_size=1000)
    application.mount("/assets", StaticFiles(directory=ROOT / "static"), name="assets")

    @application.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        path = request.url.path
        if request.method not in {"GET", "HEAD", "OPTIONS"} and path.startswith("/api/"):
            # Fail closed: a cross-site request must prove same-origin intent. Browsers
            # always send one of these on a state-changing fetch; an attacker page can
            # set neither. A missing pair is treated as hostile, not as trusted.
            fetch_site = request.headers.get("sec-fetch-site")
            origin = request.headers.get("origin")
            allowed_origin = str(request.base_url).rstrip("/")
            if fetch_site is not None:
                same_origin = fetch_site in {"same-origin", "none"}
            elif origin is not None:
                same_origin = origin.rstrip("/") == allowed_origin
            else:
                same_origin = False
            if not same_origin:
                return JSONResponse(
                    {"error": {"code": "forbidden", "message": "Request origin is not allowed", "retryable": False}},
                    status_code=403,
                )

        if path.startswith("/api/") and path not in PUBLIC_PATHS:
            database_ = request.app.state.database
            if database_.get_password_hash() and not database_.session_valid(
                request.cookies.get(SESSION_COOKIE, "")
            ):
                return JSONResponse(
                    {"error": {"code": "unauthorized", "message": "Sign in to continue", "retryable": False}},
                    status_code=401,
                )

        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "font-src 'self'; media-src 'self'; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # Assets are NOT fingerprinted: index.html hard-codes /assets/app.js and
        # /assets/style.css, so a long max-age served stale JS/CSS for up to an hour after a
        # change (observed as a fixed edit "not taking effect"). Revalidate instead -- ETag makes
        # the common case a cheap 304. Restore a long max-age only alongside hashed filenames.
        if request.url.path.startswith("/assets/") and response.status_code == 200:
            response.headers["Cache-Control"] = "no-cache"
        return response

    @application.exception_handler(KeyError)
    async def key_error_handler(_: Request, error: KeyError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "not_found", "message": str(error.args[0]), "retryable": False}},
            status_code=404,
        )

    @application.exception_handler(ValueError)
    async def value_error_handler(_: Request, error: ValueError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "invalid_request", "message": str(error), "retryable": False}},
            status_code=400,
        )

    @application.exception_handler(RPCError)
    async def rpc_error_handler(_: Request, __: RPCError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "telegram_error", "message": "Telegram rejected the request. Try again shortly.", "retryable": True}},
            status_code=502,
        )

    @application.exception_handler(httpx.HTTPError)
    async def http_error_handler(_: Request, __: httpx.HTTPError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "external_error", "message": "The music service is unavailable. Try again shortly.", "retryable": True}},
            status_code=502,
        )

    @application.exception_handler(RuntimeError)
    async def runtime_error_handler(_: Request, error: RuntimeError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "state_error", "message": str(error), "retryable": False}},
            status_code=409,
        )

    @application.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, error: HTTPException) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "http_error", "message": str(error.detail), "retryable": error.status_code >= 500}},
            status_code=error.status_code,
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "invalid_request", "message": "Check the entered values and try again.", "retryable": False}},
            status_code=422,
        )

    def database(request: Request) -> Database:
        return request.app.state.database

    def telegram(request: Request) -> TelegramService:
        return request.app.state.telegram

    def external(request: Request) -> ExternalServices:
        return request.app.state.external

    def get_track(request: Request, key: str) -> dict[str, Any]:
        chat_id, message_id = split_track_key(key)
        item = database(request).get_track(chat_id, message_id)
        if not item:
            raise KeyError("Track not found")
        return item

    @application.get("/", response_class=HTMLResponse)
    async def index() -> FileResponse:
        return FileResponse(ROOT / "static" / "index.html")

    @application.get("/sw.js")
    async def service_worker() -> FileResponse:
        return FileResponse(
            ROOT / "static" / "sw.js",
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    @application.get("/manifest.webmanifest")
    async def manifest() -> FileResponse:
        return FileResponse(
            ROOT / "static" / "manifest.webmanifest",
            media_type="application/manifest+json",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @application.get("/healthz")
    async def health(request: Request) -> dict[str, Any]:
        return {
            "status": "ok",
            "telegram": telegram(request).account_status()["linked"],
            "database": database(request).ping(),
        }

    @application.get("/api/network")
    async def network_get(request: Request) -> dict[str, Any]:
        return {
            "bindHost": str(database(request).get_settings()["bindHost"]),
            "activeHost": os.environ.get("TURNTABLE_ACTIVE_HOST", ""),
            "managed": os.environ.get("TURNTABLE_MANAGED") == "1",
            "inDocker": Path("/.dockerenv").exists(),
        }

    @application.patch("/api/network")
    async def network_save(request: Request, body: BindHostBody) -> dict[str, Any]:
        store = database(request)
        store.save_settings({"bindHost": body.bindHost})
        return {
            "bindHost": body.bindHost,
            "activeHost": os.environ.get("TURNTABLE_ACTIVE_HOST", ""),
            "managed": os.environ.get("TURNTABLE_MANAGED") == "1",
            "inDocker": Path("/.dockerenv").exists(),
            "restartRequired": body.bindHost != os.environ.get("TURNTABLE_ACTIVE_HOST", body.bindHost),
        }

    def _set_session_cookie(response: JSONResponse, token: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=settings.cookie_secure,
            path="/",
        )

    @application.get("/api/auth/status")
    async def auth_status(request: Request) -> dict[str, Any]:
        store = database(request)
        enabled = bool(store.get_password_hash())
        return {
            "passwordEnabled": enabled,
            "authenticated": not enabled
            or store.session_valid(request.cookies.get(SESSION_COOKIE, "")),
        }

    @application.post("/api/auth/login")
    async def auth_login(request: Request, body: LoginBody) -> Response:
        store = database(request)
        encoded = store.get_password_hash()
        if not encoded:
            return JSONResponse({"ok": True, "passwordEnabled": False})
        client = request.client.host if request.client else "unknown"
        if not _login_allowed(client):
            return JSONResponse(
                {"error": {"code": "rate_limited", "message": "Too many attempts. Wait a minute and try again.", "retryable": True}},
                status_code=429,
            )
        if not verify_password(body.password, encoded):
            _record_login_failure(client)
            LOGGER.warning("Failed sign-in attempt from %s", client)
            return JSONResponse(
                {"error": {"code": "unauthorized", "message": "That password is incorrect", "retryable": False}},
                status_code=401,
            )
        _clear_login_failures(client)
        response = JSONResponse({"ok": True, "passwordEnabled": True})
        _set_session_cookie(response, store.create_session())
        return response

    @application.post("/api/auth/logout")
    async def auth_logout(request: Request) -> Response:
        database(request).delete_session(request.cookies.get(SESSION_COOKIE, ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @application.post("/api/auth/password")
    async def auth_set_password(request: Request, body: PasswordBody) -> Response:
        store = database(request)
        existing = store.get_password_hash()
        if existing and not verify_password(body.current, existing):
            return JSONResponse(
                {"error": {"code": "unauthorized", "message": "That current password is incorrect", "retryable": False}},
                status_code=401,
            )
        store.set_password(body.password)
        # set_password revokes every session, including this one; re-issue so the
        # caller who just set the password is not immediately locked out.
        response = JSONResponse({"ok": True, "passwordEnabled": True})
        _set_session_cookie(response, store.create_session())
        return response

    @application.post("/api/auth/password/disable")
    async def auth_disable_password(request: Request, body: PasswordDisableBody) -> Response:
        store = database(request)
        existing = store.get_password_hash()
        if not existing:
            return JSONResponse({"ok": True, "passwordEnabled": False})
        if not verify_password(body.current, existing):
            return JSONResponse(
                {"error": {"code": "unauthorized", "message": "That password is incorrect", "retryable": False}},
                status_code=401,
            )
        store.clear_password()
        response = JSONResponse({"ok": True, "passwordEnabled": False})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @application.get("/api/status")
    async def status(request: Request) -> dict[str, Any]:
        account = telegram(request).account_status()
        return {
            "unlocked": True,
            "telegram": account,
            "startupError": request.app.state.startup_error if not account["linked"] else None,
        }

    @application.post("/api/telegram/qr")
    async def telegram_qr(request: Request) -> dict[str, Any]:
        return await telegram(request).start_qr_login()

    @application.get("/api/telegram/countries")
    async def telegram_countries(request: Request) -> list[dict[str, str]]:
        return await telegram(request).countries()

    @application.get("/api/telegram/flow/{flow_id}")
    async def telegram_flow(request: Request, flow_id: str) -> dict[str, Any]:
        return telegram(request).flow_status(flow_id)

    @application.post("/api/telegram/phone")
    async def telegram_phone(request: Request, body: PhoneBody) -> dict[str, Any]:
        return await telegram(request).start_phone_login(body.phone)

    @application.post("/api/telegram/code")
    async def telegram_code(request: Request, body: CodeBody) -> dict[str, Any]:
        return await telegram(request).submit_phone_code(body.flowId, body.code)

    @application.post("/api/telegram/password")
    async def telegram_password(request: Request, body: TelegramPasswordBody) -> dict[str, Any]:
        return await telegram(request).submit_password(body.flowId, body.password)

    @application.delete("/api/telegram/session")
    async def telegram_disconnect(request: Request) -> dict[str, bool]:
        await telegram(request).disconnect_account()
        return {"ok": True}

    @application.get("/api/sources")
    def sources(request: Request) -> list[dict[str, Any]]:
        return database(request).list_sources()

    @application.patch("/api/sources/order")
    async def source_order(request: Request, body: SourceOrderBody) -> dict[str, bool]:
        database(request).set_source_order(body.chatIds)
        return {"ok": True}

    @application.post("/api/sources/bulk-select")
    async def source_bulk_select(request: Request, body: BulkSourcesBody) -> dict[str, bool]:
        database(request).set_sources_selected(body.chatIds, body.selected)
        if not body.selected:
            for chat_id in body.chatIds:
                await telegram(request).set_source_selected(chat_id, False)
        return {"ok": True}

    @application.get("/api/sources/discover")
    async def discover(request: Request) -> list[dict[str, Any]]:
        return await telegram(request).discover_sources()

    @application.post("/api/sources/discover/counts")
    async def discover_counts(request: Request) -> dict[str, Any]:
        sources = await telegram(request).discover_sources()
        return telegram(request).start_source_counts(sources)

    @application.post("/api/sources")
    async def source_add(request: Request, body: SourceBody) -> dict[str, Any]:
        return await telegram(request).add_source(body.chatId)

    @application.patch("/api/sources/{chat_id}")
    async def source_select(
        request: Request, chat_id: str, body: SourceSelectionBody
    ) -> dict[str, Any]:
        return await telegram(request).set_source_selected(chat_id, body.selected)

    @application.patch("/api/sources/{chat_id}/pin")
    def source_pin(request: Request, chat_id: str, body: SourcePinBody) -> dict[str, Any]:
        database(request).set_source_pinned(chat_id, body.pinned)
        return {"ok": True}

    @application.delete("/api/sources/{chat_id}")
    async def source_remove(request: Request, chat_id: str) -> dict[str, bool]:
        if not database(request).get_source(chat_id):
            raise KeyError("Source not found")
        await telegram(request).set_source_selected(chat_id, False)
        return {"ok": True}

    @application.post("/api/sources/{chat_id}/sync")
    async def source_sync(request: Request, chat_id: str, body: SyncBody) -> dict[str, Any]:
        return telegram(request).start_sync(chat_id, full=body.full)

    @application.post("/api/sources/sync-all")
    async def sources_sync_all(request: Request) -> dict[str, bool]:
        await telegram(request).sync_all()
        return {"ok": True}

    @application.post("/api/sources/{chat_id}/preview")
    async def source_preview(request: Request, chat_id: str) -> dict[str, Any]:
        return telegram(request).start_preview(chat_id)

    @application.get("/api/jobs/{job_id}")
    async def job_status(request: Request, job_id: str) -> dict[str, Any]:
        return telegram(request).job_status(job_id)

    @application.delete("/api/jobs/{job_id}")
    async def job_cancel(request: Request, job_id: str) -> dict[str, Any]:
        return telegram(request).cancel_job(job_id)

    @application.get("/api/sources/{chat_id}/avatar")
    async def source_avatar(request: Request, chat_id: str) -> Response:
        content = await telegram(request).avatar(chat_id)
        if not content:
            return Response(status_code=404, headers={"Cache-Control": "private, max-age=86400"})
        return Response(content, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @application.get("/api/tracks")
    def tracks(
        request: Request,
        source: str | None = None,
        q: str = "",
        offset: int = 0,
        limit: int = 100,
        liked: bool = False,
        temporary: bool = False,
        total: int | None = None,
    ) -> dict[str, Any]:
        # ponytail: the client already knows the total after the first page of a given filter
        # and echoes it back, which lets us skip the COUNT(*) entirely while scrolling.
        return database(request).list_tracks(
            source, q[:200], offset, limit, liked, temporary, total if total is not None and total >= 0 else None
        )

    @application.post("/api/tracks/summaries")
    def track_summaries(request: Request, body: TrackKeysBody) -> dict[str, Any]:
        return {"items": database(request).track_summaries(body.keys)}

    @application.get("/api/library/stats")
    def library_stats(request: Request) -> dict[str, int]:
        return {"likedCount": database(request).liked_count()}

    @application.post("/api/search/telegram")
    async def telegram_search(request: Request, body: TelegramSearchBody) -> dict[str, Any]:
        return await telegram(request).global_music_search(body.query, body.limit)

    @application.get("/api/tracks/{key}/position")
    def track_position(
        request: Request,
        key: str,
        source: str | None = None,
        q: str = "",
        liked: bool = False,
        temporary: bool = False,
    ) -> dict[str, int]:
        return {
            "index": database(request).track_position(
                key, source, q[:200], liked, temporary
            )
        }

    @application.get("/api/tracks/{key}")
    async def track_detail(request: Request, key: str) -> dict[str, Any]:
        return get_track(request, key)

    @application.patch("/api/tracks/{key}/like")
    def track_like(request: Request, key: str, body: LikeBody) -> dict[str, Any]:
        get_track(request, key)
        return database(request).set_liked(key, body.liked)

    @application.get("/api/telegram/contacts")
    async def telegram_contacts(request: Request) -> list[dict[str, Any]]:
        return await telegram(request).contacts()

    @application.post("/api/tracks/{key}/saved-messages")
    async def save_to_telegram(request: Request, key: str) -> dict[str, Any]:
        return await telegram(request).forward_track(get_track(request, key))

    @application.post("/api/tracks/{key}/share")
    async def share_track(request: Request, key: str, body: ShareBody) -> dict[str, Any]:
        return await telegram(request).forward_track(get_track(request, key), body.recipientId)

    async def media_response(request: Request, key: str, download: bool = False) -> Response:
        item = get_track(request, key)
        chat_id, message_id = item["chatId"], item["messageId"]
        cached = telegram(request).cached_media(item)
        document = None
        if cached:
            size = cached.stat().st_size
        else:
            _, document = await telegram(request).get_message_document(chat_id, message_id)
            size = int(document.size or item["file"]["size"] or 0)
        try:
            byte_range = parse_range_header(None if download else request.headers.get("range"), size)
        except RangeNotSatisfiable as error:
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{size}"},
                content=str(error),
            )
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(byte_range.length),
            "Cache-Control": "private, no-store",
            # ponytail: pre-set Content-Encoding so Starlette GZipMiddleware
            # (which skips bodies when the header is already present) leaves
            # the byte stream alone. Without this, audio bytes get gzipped
            # and Content-Range semantics break.
            "Content-Encoding": "identity",
        }
        status_code = 206 if byte_range.partial else 200
        if byte_range.partial:
            headers["Content-Range"] = f"bytes {byte_range.start}-{byte_range.end}/{size}"
        if download:
            name = safe_filename(item["file"]["name"], "telegram-track")
            ascii_name = safe_filename(name.encode("ascii", "ignore").decode() or "telegram-track")
            headers["Content-Disposition"] = (
                f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name)}'
            )
        if request.method == "HEAD":
            return Response(status_code=status_code, media_type=item["file"]["mimeType"], headers=headers)
        if cached:
            def cached_chunks():
                remaining = byte_range.length
                with cached.open("rb") as source_file:
                    source_file.seek(byte_range.start)
                    while remaining:
                        chunk = source_file.read(min(512 * 1024, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk

            content = cached_chunks()
        else:
            content = telegram(request).iter_media(document, byte_range.start, byte_range.length)
        return StreamingResponse(
            content,
            status_code=status_code,
            media_type=item["file"]["mimeType"],
            headers=headers,
        )

    @application.api_route(
        "/api/tracks/{key}/audio", methods=["GET", "HEAD"]    )
    async def audio(request: Request, key: str) -> Response:
        return await media_response(request, key)

    @application.get("/api/tracks/{key}/download")
    async def download(request: Request, key: str) -> Response:
        item = get_track(request, key)
        if tagged := await telegram(request).tagged_download(item):
            name = safe_filename(item["file"]["name"], "telegram-track")
            ascii_name = safe_filename(name.encode("ascii", "ignore").decode() or "telegram-track")
            return FileResponse(
                tagged,
                media_type=item["file"]["mimeType"],
                filename=ascii_name,
                headers={
                    "Cache-Control": "private, no-store",
                    "Content-Disposition": f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name)}',
                    "Content-Encoding": "identity",
                },
            )
        return await media_response(request, key, download=True)

    @application.get("/api/tracks/{key}/cover")
    async def cover(request: Request, key: str, quality: str = "default") -> Response:
        item = get_track(request, key)
        artwork = item["metadata"].get("artworkPath")
        if artwork:
            candidate = (settings.data_directory / "artwork" / Path(artwork).name).resolve()
            root = (settings.data_directory / "artwork").resolve()
            if candidate.parent == root and candidate.is_file():
                return FileResponse(
                    candidate,
                    headers={"Cache-Control": "private, max-age=86400", "Content-Encoding": "identity"},
                )
        thumbnail = await telegram(request).thumbnail(item["chatId"], item["messageId"], quality=quality)
        if not thumbnail:
            return Response(
                status_code=404,
                headers={"Cache-Control": "private, max-age=86400", "Content-Encoding": "identity"},
            )
        return Response(
            thumbnail,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=86400", "Content-Encoding": "identity"},
        )

    @application.patch("/api/tracks/{key}/metadata")
    async def metadata_patch(request: Request, key: str, body: MetadataPatchBody) -> dict[str, Any]:
        item = get_track(request, key)
        return database(request).save_metadata_patch(
            item["chatId"], item["messageId"], body.set, body.clear
        )

    @application.post("/api/tracks/{key}/metadata/search")
    async def metadata_search(
        request: Request, key: str, body: MetadataSearchBody
    ) -> list[dict[str, Any]]:
        return await external(request).metadata_candidates(get_track(request, key), body.refresh)

    @application.post("/api/tracks/{key}/metadata/apply")
    async def metadata_apply(request: Request, key: str, body: CandidateBody) -> dict[str, Any]:
        return await external(request).apply_candidate(
            get_track(request, key),
            body.candidateId,
            body.fields,
            body.coverQuality or str(database(request).get_settings()["coverQuality"]),
        )

    @application.get(
        "/api/tracks/{key}/metadata/candidates/{candidate_id}/cover",
    )
    async def metadata_candidate_cover(request: Request, key: str, candidate_id: str) -> Response:
        content, mime_type = await external(request).candidate_cover(
            get_track(request, key), candidate_id
        )
        return Response(content, media_type=mime_type, headers={"Cache-Control": "private, max-age=3600"})

    @application.get("/api/tracks/{key}/lyrics")
    async def lyrics_get(request: Request, key: str, refresh: bool = False) -> dict[str, Any]:
        return await external(request).lyrics(get_track(request, key), refresh)

    @application.put("/api/tracks/{key}/lyrics")
    async def lyrics_save(request: Request, key: str, body: LyricsBody) -> dict[str, Any]:
        return external(request).save_manual_lyrics(get_track(request, key), body.text)

    @application.delete("/api/tracks/{key}/lyrics")
    async def lyrics_reset(request: Request, key: str) -> dict[str, Any]:
        item = get_track(request, key)
        database(request).delete_lyrics(item["chatId"], item["messageId"])
        return await external(request).lyrics(item, refresh=True)

    @application.get("/api/settings")
    async def settings_get(request: Request) -> dict[str, Any]:
        return database(request).get_settings()

    @application.patch("/api/settings")
    async def settings_save(request: Request, body: SettingsBody) -> dict[str, Any]:
        return database(request).save_settings(body.model_dump(exclude_none=True))

    @application.post(
        "/api/settings/musicbrainz/test"    )
    async def settings_musicbrainz_test(request: Request) -> dict[str, bool]:
        return await external(request).test_musicbrainz()

    @application.post("/api/playback/events")
    async def playback_event(request: Request, body: PlaybackEventBody) -> dict[str, bool]:
        get_track(request, body.key)
        database(request).record_playback(body.key, body.event)
        return {"ok": True}

    @application.post("/api/playback/shuffle")
    def playback_shuffle(request: Request, body: ShuffleBody) -> dict[str, Any]:
        return {
            "keys": database(request).shuffled_track_keys(body.source, body.currentKey),
        }

    @application.post("/api/playback/queue")
    def playback_queue(request: Request, body: QueueBody) -> dict[str, Any]:
        return {
            "keys": database(request).playback_queue(
                body.source, body.query, body.shuffle, body.currentKey,
                body.liked, body.temporary,
            )
        }

    @application.post("/api/playback/prefetch")
    async def playback_prefetch(request: Request, body: PrefetchBody) -> dict[str, Any]:
        for key in body.keys:
            get_track(request, key)
        return telegram(request).start_prefetch(body.keys)

    @application.get("/api/cache/status")
    async def cache_status(request: Request, keys: str = "") -> dict[str, Any]:
        return telegram(request).cache_status([value for value in keys.split(",") if value])

    @application.delete("/api/cache")
    async def cache_clear(request: Request) -> dict[str, int]:
        return telegram(request).clear_media_cache()

    return application


app = create_app()
