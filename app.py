from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from telethon.errors import RPCError

from core import (
    ByteRange,
    Database,
    RangeNotSatisfiable,
    parse_range_header,
    safe_filename,
    split_track_key,
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
    app_password: str
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
            app_password=os.environ.get("APP_PASSWORD", ""),
            encryption_key=encryption_key,
            data_directory=data_directory,
            musicbrainz_contact=os.environ.get("MUSICBRAINZ_CONTACT", ""),
            cookie_secure=os.environ.get("DEV_INSECURE_COOKIE") != "1",
        )


class PasswordBody(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


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


def _cookie_key(settings: Settings) -> bytes:
    return hashlib.sha256(b"telegram-music-cookie\0" + settings.encryption_key.encode()).digest()


def _make_cookie(settings: Settings) -> str:
    expires = int(time.time()) + 30 * 24 * 60 * 60
    payload = f"{expires}.{secrets.token_urlsafe(24)}"
    signature = hmac.new(_cookie_key(settings), payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def _valid_cookie(settings: Settings, value: str | None) -> bool:
    if not value:
        return False
    payload, separator, signature_b64 = value.rpartition(".")
    if not separator or not signature_b64:
        return False
    expires_str, sep, token = payload.partition(".")
    if not sep or not token or not expires_str:
        return False
    try:
        expires = int(expires_str)
    except ValueError:
        return False
    if expires < int(time.time()):
        return False
    try:
        padding = "=" * (-len(signature_b64) % 4)
        signature = base64.urlsafe_b64decode(signature_b64 + padding)
        expected = hmac.new(_cookie_key(settings), payload.encode(), hashlib.sha256).digest()
    except Exception:
        return False
    return hmac.compare_digest(signature, expected)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    login_attempts: dict[str, list[float]] = {}

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

    application = FastAPI(title="Telegram Music", docs_url=None, redoc_url=None, lifespan=lifespan)
    application.add_middleware(GZipMiddleware, minimum_size=1000)
    application.mount("/assets", StaticFiles(directory=ROOT / "static"), name="assets")

    @application.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "Request origin is not allowed"}, status_code=403)
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
        # ponytail: rotate the session cookie on each authenticated /api/ call so a leaked
        # token stops being useful after the legitimate user's next request; skipped on
        # static assets (path != /api/) and the logout endpoint (would re-issue what it
        # just deleted)
        if (
            request.url.path.startswith("/api/")
            and request.url.path != "/api/access/session"
            and _valid_cookie(settings, request.cookies.get("tm_session"))
        ):
            response.set_cookie(
                "tm_session",
                _make_cookie(settings),
                max_age=30 * 24 * 60 * 60,
                httponly=True,
                secure=settings.cookie_secure,
                samesite="strict",
                path="/",
            )
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

    def require_access(request: Request) -> None:
        if not _valid_cookie(settings, request.cookies.get("tm_session")):
            raise HTTPException(status_code=401, detail="Unlock the app first")

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

    @application.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/api/status")
    async def status(request: Request) -> dict[str, Any]:
        unlocked = _valid_cookie(settings, request.cookies.get("tm_session"))
        account = telegram(request).account_status() if unlocked else {"linked": False}
        return {
            "unlocked": unlocked,
            "telegram": account,
            "startupError": request.app.state.startup_error if unlocked and not account["linked"] else None,
        }

    @application.post("/api/access/login")
    async def access_login(request: Request, body: PasswordBody) -> Response:
        client_ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        # ponytail: full sweep when dict exceeds 1000 entries; per-bucket pruning handles returning IPs
        if len(login_attempts) > 1000:
            for ip in list(login_attempts):
                login_attempts[ip] = [t for t in login_attempts[ip] if now - t < 15 * 60]
                if not login_attempts[ip]:
                    del login_attempts[ip]
        recent = [stamp for stamp in login_attempts.get(client_ip, []) if now - stamp < 15 * 60]
        if len(recent) >= 5:
            raise HTTPException(status_code=429, detail="Too many attempts; retry in 15 minutes")
        if recent:
            login_attempts[client_ip] = recent
        else:
            login_attempts.pop(client_ip, None)
        if not hmac.compare_digest(body.password.encode(), settings.app_password.encode()):
            recent.append(now)
            login_attempts[client_ip] = recent
            raise HTTPException(status_code=401, detail="The app password is incorrect")
        login_attempts.pop(client_ip, None)
        response = JSONResponse({"ok": True})
        response.set_cookie(
            "tm_session",
            _make_cookie(settings),
            max_age=30 * 24 * 60 * 60,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            path="/",
        )
        return response

    @application.delete("/api/access/session", dependencies=[Depends(require_access)])
    async def access_logout() -> Response:
        response = JSONResponse({"ok": True})
        response.delete_cookie("tm_session", path="/")
        return response

    @application.post("/api/telegram/qr", dependencies=[Depends(require_access)])
    async def telegram_qr(request: Request) -> dict[str, Any]:
        return await telegram(request).start_qr_login()

    @application.get("/api/telegram/countries", dependencies=[Depends(require_access)])
    async def telegram_countries(request: Request) -> list[dict[str, str]]:
        return await telegram(request).countries()

    @application.get("/api/telegram/flow/{flow_id}", dependencies=[Depends(require_access)])
    async def telegram_flow(request: Request, flow_id: str) -> dict[str, Any]:
        return telegram(request).flow_status(flow_id)

    @application.post("/api/telegram/phone", dependencies=[Depends(require_access)])
    async def telegram_phone(request: Request, body: PhoneBody) -> dict[str, Any]:
        return await telegram(request).start_phone_login(body.phone)

    @application.post("/api/telegram/code", dependencies=[Depends(require_access)])
    async def telegram_code(request: Request, body: CodeBody) -> dict[str, Any]:
        return await telegram(request).submit_phone_code(body.flowId, body.code)

    @application.post("/api/telegram/password", dependencies=[Depends(require_access)])
    async def telegram_password(request: Request, body: TelegramPasswordBody) -> dict[str, Any]:
        return await telegram(request).submit_password(body.flowId, body.password)

    @application.delete("/api/telegram/session", dependencies=[Depends(require_access)])
    async def telegram_disconnect(request: Request) -> dict[str, bool]:
        await telegram(request).disconnect_account()
        return {"ok": True}

    @application.get("/api/sources", dependencies=[Depends(require_access)])
    def sources(request: Request) -> list[dict[str, Any]]:
        return database(request).list_sources()

    @application.patch("/api/sources/order", dependencies=[Depends(require_access)])
    async def source_order(request: Request, body: SourceOrderBody) -> dict[str, bool]:
        database(request).set_source_order(body.chatIds)
        return {"ok": True}

    @application.post("/api/sources/bulk-select", dependencies=[Depends(require_access)])
    async def source_bulk_select(request: Request, body: BulkSourcesBody) -> dict[str, bool]:
        database(request).set_sources_selected(body.chatIds, body.selected)
        if not body.selected:
            for chat_id in body.chatIds:
                await telegram(request).set_source_selected(chat_id, False)
        return {"ok": True}

    @application.get("/api/sources/discover", dependencies=[Depends(require_access)])
    async def discover(request: Request) -> list[dict[str, Any]]:
        return await telegram(request).discover_sources()

    @application.post("/api/sources/discover/counts", dependencies=[Depends(require_access)])
    async def discover_counts(request: Request) -> dict[str, Any]:
        sources = await telegram(request).discover_sources()
        return telegram(request).start_source_counts(sources)

    @application.post("/api/sources", dependencies=[Depends(require_access)])
    async def source_add(request: Request, body: SourceBody) -> dict[str, Any]:
        return await telegram(request).add_source(body.chatId)

    @application.patch("/api/sources/{chat_id}", dependencies=[Depends(require_access)])
    async def source_select(
        request: Request, chat_id: str, body: SourceSelectionBody
    ) -> dict[str, Any]:
        return await telegram(request).set_source_selected(chat_id, body.selected)

    @application.delete("/api/sources/{chat_id}", dependencies=[Depends(require_access)])
    async def source_remove(request: Request, chat_id: str) -> dict[str, bool]:
        if not database(request).get_source(chat_id):
            raise KeyError("Source not found")
        await telegram(request).set_source_selected(chat_id, False)
        return {"ok": True}

    @application.post("/api/sources/{chat_id}/sync", dependencies=[Depends(require_access)])
    async def source_sync(request: Request, chat_id: str, body: SyncBody) -> dict[str, Any]:
        return telegram(request).start_sync(chat_id, full=body.full)

    @application.post("/api/sources/sync-all", dependencies=[Depends(require_access)])
    async def sources_sync_all(request: Request) -> dict[str, bool]:
        await telegram(request).sync_all()
        return {"ok": True}

    @application.post("/api/sources/{chat_id}/preview", dependencies=[Depends(require_access)])
    async def source_preview(request: Request, chat_id: str) -> dict[str, Any]:
        return telegram(request).start_preview(chat_id)

    @application.get("/api/jobs/{job_id}", dependencies=[Depends(require_access)])
    async def job_status(request: Request, job_id: str) -> dict[str, Any]:
        return telegram(request).job_status(job_id)

    @application.delete("/api/jobs/{job_id}", dependencies=[Depends(require_access)])
    async def job_cancel(request: Request, job_id: str) -> dict[str, Any]:
        return telegram(request).cancel_job(job_id)

    @application.get("/api/sources/{chat_id}/avatar", dependencies=[Depends(require_access)])
    async def source_avatar(request: Request, chat_id: str) -> Response:
        content = await telegram(request).avatar(chat_id)
        if not content:
            return Response(status_code=404, headers={"Cache-Control": "private, max-age=86400"})
        return Response(content, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @application.get("/api/tracks", dependencies=[Depends(require_access)])
    def tracks(
        request: Request,
        source: str | None = None,
        q: str = "",
        offset: int = 0,
        limit: int = 100,
        liked: bool = False,
        temporary: bool = False,
    ) -> dict[str, Any]:
        return database(request).list_tracks(
            source, q[:200], offset, limit, liked, temporary
        )

    @application.post("/api/tracks/summaries", dependencies=[Depends(require_access)])
    def track_summaries(request: Request, body: TrackKeysBody) -> dict[str, Any]:
        return {"items": database(request).track_summaries(body.keys)}

    @application.get("/api/library/stats", dependencies=[Depends(require_access)])
    def library_stats(request: Request) -> dict[str, int]:
        return {"likedCount": database(request).liked_count()}

    @application.post("/api/search/telegram", dependencies=[Depends(require_access)])
    async def telegram_search(request: Request, body: TelegramSearchBody) -> dict[str, Any]:
        return await telegram(request).global_music_search(body.query, body.limit)

    @application.get("/api/tracks/{key}/position", dependencies=[Depends(require_access)])
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

    @application.get("/api/tracks/{key}", dependencies=[Depends(require_access)])
    async def track_detail(request: Request, key: str) -> dict[str, Any]:
        return get_track(request, key)

    @application.patch("/api/tracks/{key}/like", dependencies=[Depends(require_access)])
    def track_like(request: Request, key: str, body: LikeBody) -> dict[str, Any]:
        get_track(request, key)
        return database(request).set_liked(key, body.liked)

    @application.get("/api/telegram/contacts", dependencies=[Depends(require_access)])
    async def telegram_contacts(request: Request) -> list[dict[str, Any]]:
        return await telegram(request).contacts()

    @application.post("/api/tracks/{key}/saved-messages", dependencies=[Depends(require_access)])
    async def save_to_telegram(request: Request, key: str) -> dict[str, Any]:
        return await telegram(request).forward_track(get_track(request, key))

    @application.post("/api/tracks/{key}/share", dependencies=[Depends(require_access)])
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
        "/api/tracks/{key}/audio", methods=["GET", "HEAD"], dependencies=[Depends(require_access)]
    )
    async def audio(request: Request, key: str) -> Response:
        return await media_response(request, key)

    @application.get("/api/tracks/{key}/download", dependencies=[Depends(require_access)])
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
                },
            )
        return await media_response(request, key, download=True)

    @application.get("/api/tracks/{key}/cover", dependencies=[Depends(require_access)])
    async def cover(request: Request, key: str, quality: str = "default") -> Response:
        item = get_track(request, key)
        artwork = item["metadata"].get("artworkPath")
        if artwork:
            candidate = (settings.data_directory / "artwork" / Path(artwork).name).resolve()
            root = (settings.data_directory / "artwork").resolve()
            if candidate.parent == root and candidate.is_file():
                return FileResponse(candidate, headers={"Cache-Control": "private, max-age=86400"})
        thumbnail = await telegram(request).thumbnail(item["chatId"], item["messageId"], quality=quality)
        if not thumbnail:
            return Response(status_code=404, headers={"Cache-Control": "private, max-age=86400"})
        return Response(thumbnail, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @application.patch("/api/tracks/{key}/metadata", dependencies=[Depends(require_access)])
    async def metadata_patch(request: Request, key: str, body: MetadataPatchBody) -> dict[str, Any]:
        item = get_track(request, key)
        return database(request).save_metadata_patch(
            item["chatId"], item["messageId"], body.set, body.clear
        )

    @application.post("/api/tracks/{key}/metadata/search", dependencies=[Depends(require_access)])
    async def metadata_search(
        request: Request, key: str, body: MetadataSearchBody
    ) -> list[dict[str, Any]]:
        return await external(request).metadata_candidates(get_track(request, key), body.refresh)

    @application.post("/api/tracks/{key}/metadata/apply", dependencies=[Depends(require_access)])
    async def metadata_apply(request: Request, key: str, body: CandidateBody) -> dict[str, Any]:
        return await external(request).apply_candidate(
            get_track(request, key),
            body.candidateId,
            body.fields,
            body.coverQuality or str(database(request).get_settings()["coverQuality"]),
        )

    @application.get(
        "/api/tracks/{key}/metadata/candidates/{candidate_id}/cover",
        dependencies=[Depends(require_access)],
    )
    async def metadata_candidate_cover(request: Request, key: str, candidate_id: str) -> Response:
        content, mime_type = await external(request).candidate_cover(
            get_track(request, key), candidate_id
        )
        return Response(content, media_type=mime_type, headers={"Cache-Control": "private, max-age=3600"})

    @application.get("/api/tracks/{key}/lyrics", dependencies=[Depends(require_access)])
    async def lyrics_get(request: Request, key: str, refresh: bool = False) -> dict[str, Any]:
        return await external(request).lyrics(get_track(request, key), refresh)

    @application.put("/api/tracks/{key}/lyrics", dependencies=[Depends(require_access)])
    async def lyrics_save(request: Request, key: str, body: LyricsBody) -> dict[str, Any]:
        return external(request).save_manual_lyrics(get_track(request, key), body.text)

    @application.delete("/api/tracks/{key}/lyrics", dependencies=[Depends(require_access)])
    async def lyrics_reset(request: Request, key: str) -> dict[str, Any]:
        item = get_track(request, key)
        database(request).delete_lyrics(item["chatId"], item["messageId"])
        return await external(request).lyrics(item, refresh=True)

    @application.get("/api/settings", dependencies=[Depends(require_access)])
    async def settings_get(request: Request) -> dict[str, Any]:
        return database(request).get_settings()

    @application.patch("/api/settings", dependencies=[Depends(require_access)])
    async def settings_save(request: Request, body: SettingsBody) -> dict[str, Any]:
        return database(request).save_settings(body.model_dump(exclude_none=True))

    @application.post(
        "/api/settings/musicbrainz/test", dependencies=[Depends(require_access)]
    )
    async def settings_musicbrainz_test(request: Request) -> dict[str, bool]:
        return await external(request).test_musicbrainz()

    @application.post("/api/playback/events", dependencies=[Depends(require_access)])
    async def playback_event(request: Request, body: PlaybackEventBody) -> dict[str, bool]:
        get_track(request, body.key)
        database(request).record_playback(body.key, body.event)
        return {"ok": True}

    @application.post("/api/playback/shuffle", dependencies=[Depends(require_access)])
    def playback_shuffle(request: Request, body: ShuffleBody) -> dict[str, Any]:
        return {
            "keys": database(request).shuffled_track_keys(body.source, body.currentKey),
        }

    @application.post("/api/playback/queue", dependencies=[Depends(require_access)])
    def playback_queue(request: Request, body: QueueBody) -> dict[str, Any]:
        return {
            "keys": database(request).playback_queue(
                body.source, body.query, body.shuffle, body.currentKey,
                body.liked, body.temporary,
            )
        }

    @application.post("/api/playback/prefetch", dependencies=[Depends(require_access)])
    async def playback_prefetch(request: Request, body: PrefetchBody) -> dict[str, Any]:
        for key in body.keys:
            get_track(request, key)
        return telegram(request).start_prefetch(body.keys)

    @application.get("/api/cache/status", dependencies=[Depends(require_access)])
    async def cache_status(request: Request, keys: str = "") -> dict[str, Any]:
        return telegram(request).cache_status([value for value in keys.split(",") if value])

    @application.delete("/api/cache", dependencies=[Depends(require_access)])
    async def cache_clear(request: Request) -> dict[str, int]:
        return telegram(request).clear_media_cache()

    return application


app = create_app()
