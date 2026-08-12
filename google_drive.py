"""Google Drive sync provider (Phase K): appDataFolder, per-entity records.

The provider speaks the SyncProvider protocol from sync.py against Google's
Drive API restricted to the application-data area (K2): the user's normal
Drive is never touched, only the hidden appDataFolder reserved for this
app. Records are stored one object per entity (K1) -- writing a like never
rewrites the whole state.

OAuth is an authorization-code flow with the narrow app-data scope; the
refresh token is stored encrypted by the app's Fernet key (26.3) and never
travels inside a sync record.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any, Callable

import httpx
from cryptography.fernet import Fernet, InvalidToken

from sync import ChangeBatch, WriteResult

DRIVE_APP_DATA_SCOPE = "https://www.googleapis.com/auth/drive.appdata"
DRIVE_FILES_ENDPOINT = "https://www.googleapis.com/drive/v3/files"
DRIVE_AUTH_ENDPOINT = "https://oauth2.googleapis.com/token"
DRIVE_AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
DRIVE_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class GoogleSyncManager:
    """OAuth + token lifecycle for the Drive provider (K2, K4, 26.3).

    The refresh token never enters a sync record: it lives in sync_state,
    Fernet-encrypted with the app's own key (which is already 0600 on disk),
    and the provider only ever receives a fresh bearer token.
    """

    def __init__(
        self,
        database: Any,
        fernet: Fernet,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        http: httpx.AsyncClient,
    ):
        self.database = database
        self.fernet = fernet
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.http = http

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def connect_url(self) -> str:
        state = secrets.token_urlsafe(16)
        self.database.sync_state_set("drive_oauth_state", state)
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": DRIVE_APP_DATA_SCOPE,
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
        query = "&".join(f"{key}={_urlencode(value)}" for key, value in params.items())
        return f"{DRIVE_AUTHORIZE_ENDPOINT}?{query}"

    def _load_token(self) -> dict[str, Any] | None:
        encrypted = self.database.sync_state_get("drive_token")
        if not encrypted:
            return None
        try:
            return json.loads(self.fernet.decrypt(encrypted.encode()).decode())
        except (InvalidToken, ValueError):
            return None

    def _save_token(self, token: dict[str, Any]) -> None:
        self.database.sync_state_set(
            "drive_token", self.fernet.encrypt(json.dumps(token).encode()).decode()
        )

    async def complete_login(self, code: str, state: str) -> str:
        """Exchange the authorization code; returns the Google account email."""
        if state != self.database.sync_state_get("drive_oauth_state"):
            raise GoogleDriveError("OAuth state mismatch; start the connection again")
        try:
            response = await self.http.post(
                DRIVE_AUTH_ENDPOINT,
                data={
                    "code": code,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "redirect_uri": self.redirect_uri,
                    "grant_type": "authorization_code",
                },
                timeout=30,
            )
        except httpx.HTTPError as error:
            raise GoogleDriveError(f"Token exchange failed: {error}") from error
        if response.status_code >= 400:
            raise GoogleDriveError(f"Google rejected the token exchange ({response.status_code})")
        token = response.json()
        self._save_token({
            "refresh_token": token.get("refresh_token", ""),
            "access_token": token.get("access_token", ""),
            "expires_at": int(time.time()) + int(token.get("expires_in") or 3600),
            "email": "",
        })
        self.database.sync_state_set("drive_oauth_state", "")
        # Resolve the account email via the token info endpoint (best effort).
        try:
            info = await self.http.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {token.get('access_token', '')}"},
                timeout=15,
            )
            if info.status_code == 200:
                stored = self._load_token()
                stored["email"] = info.json().get("email", "")
                self._save_token(stored)
                return stored["email"]
        except httpx.HTTPError:
            pass
        return "Google account"

    async def get_access_token(self) -> str:
        token = self._load_token()
        if not token or not token.get("refresh_token"):
            raise GoogleDriveError("Google Drive is not connected")
        if token.get("access_token") and int(token.get("expires_at") or 0) > time.time() + 60:
            return token["access_token"]
        try:
            response = await self.http.post(
                DRIVE_AUTH_ENDPOINT,
                data={
                    "refresh_token": token["refresh_token"],
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "refresh_token",
                },
                timeout=30,
            )
        except httpx.HTTPError as error:
            raise GoogleDriveError(f"Token refresh failed: {error}") from error
        if response.status_code >= 400:
            raise GoogleDriveError("Google rejected the token refresh; reconnect")
        body = response.json()
        if not body.get("access_token"):
            raise GoogleDriveError("Google rejected the token refresh; reconnect")
        token["access_token"] = body["access_token"]
        token["expires_at"] = int(time.time()) + int(body.get("expires_in") or 3600)
        self._save_token(token)
        return token["access_token"]

    def connected(self) -> bool:
        token = self._load_token()
        return bool(token and token.get("refresh_token"))

    def email(self) -> str:
        token = self._load_token()
        return str(token.get("email") or "") if token else ""

    def build_provider(self) -> GoogleDriveProvider:
        return GoogleDriveProvider(self.http, self.get_access_token)

    async def disconnect(self) -> None:
        self.database.sync_state_set("drive_token", "")
        self.database.sync_state_set("drive_oauth_state", "")


def _urlencode(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


class GoogleDriveError(RuntimeError):
    """Transient provider failure (network, rate limit, server error)."""


def _escape_q(value: str) -> str:
    """Escape a value for a Drive files.list q expression (single-quoted)."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveProvider:
    """SyncProvider over the Drive appDataFolder.

    *get_access_token* returns a valid bearer token, refreshing it when the
    stored one is expired (the app owns the token lifecycle).
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        get_access_token: Callable[[], Any],
    ):
        self.http = http
        self.get_access_token = get_access_token

    async def _request(
        self, method: str, url: str, *, params: dict[str, Any] | None = None,
        content: bytes | None = None, headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        token = await self.get_access_token()
        try:
            response = await self.http.request(
                method,
                url,
                params=params,
                content=content,
                headers={
                    "Authorization": f"Bearer {token}",
                    **(headers or {}),
                },
            )
        except httpx.HTTPError as error:
            raise GoogleDriveError(f"Drive request failed: {error}") from error
        if response.status_code in DRIVE_RETRYABLE_STATUS:
            raise GoogleDriveError(f"Drive server error: {response.status_code}")
        if response.status_code >= 400:
            detail = response.text[:200]
            raise GoogleDriveError(f"Drive rejected the request ({response.status_code}): {detail}")
        return response

    async def _find_by_name(self, name: str) -> dict[str, Any] | None:
        response = await self._request(
            "GET",
            DRIVE_FILES_ENDPOINT,
            params={
                "spaces": "appDataFolder",
                "q": f"name = '{_escape_q(name)}' and trashed = false",
                "fields": "files(id, name, appProperties)",
                "pageSize": "10",
            },
        )
        files = response.json().get("files") or []
        return files[0] if files else None

    async def _list_files(self) -> list[dict[str, Any]]:
        response = await self._request(
            "GET",
            DRIVE_FILES_ENDPOINT,
            params={
                "spaces": "appDataFolder",
                "q": "trashed = false",
                "fields": "files(id, name, appProperties)",
                "pageSize": "1000",
            },
        )
        return response.json().get("files") or []

    async def read_object(self, name: str) -> bytes | None:
        existing = await self._find_by_name(name)
        if existing is None:
            return None
        response = await self._request(
            "GET", f"{DRIVE_FILES_ENDPOINT}/{existing['id']}", params={"alt": "media"}
        )
        return response.content

    async def write_object(self, name: str, data: bytes, etag: str | None = None) -> WriteResult:
        revision = str(int(time.time() * 1000))
        existing = await self._find_by_name(name)
        if existing is None:
            await self._request(
                "POST",
                DRIVE_FILES_ENDPOINT,
                params={
                    "uploadType": "multipart",
                    "spaces": "appDataFolder",
                },
                headers={"Content-Type": "application/json"},
                content=_multipart(name, revision, data),
            )
        else:
            await self._request(
                "PATCH",
                f"{DRIVE_FILES_ENDPOINT}/{existing['id']}",
                params={
                    "uploadType": "multipart",
                },
                headers={"Content-Type": "application/json"},
                content=_multipart(name, revision, data),
            )
        return WriteResult(ok=True, etag=revision)

    async def list_changes(self, cursor: str | None) -> ChangeBatch:
        """Every record newer than *cursor* (a millisecond revision), plus the new cursor.

        The revision rides in the file's appProperties, so the cursor never
        requires reading file contents.
        """
        latest = int(cursor or 0)
        files = await self._list_files()
        records: list[dict[str, Any]] = []
        for item in files:
            revision = int((item.get("appProperties") or {}).get("rev") or 0)
            if revision <= latest:
                continue
            response = await self._request(
                "GET", f"{DRIVE_FILES_ENDPOINT}/{item['id']}", params={"alt": "media"}
            )
            try:
                records.append(json.loads(response.content.decode("utf-8")))
            except (ValueError, UnicodeDecodeError):
                continue
            latest = max(latest, revision)
        return ChangeBatch(records=records, next_cursor=str(latest) if files else cursor)


def _multipart(name: str, revision: str, data: bytes) -> bytes:
    """A minimal multipart/related body for Drive uploadType=multipart."""
    boundary = "turntable-sync-boundary"
    metadata = json.dumps(
        {"name": name, "appProperties": {"rev": revision}}, ensure_ascii=False
    ).encode("utf-8")
    parts = [
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode(),
        metadata,
        b"\r\n",
        f"--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n".encode(),
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts)
