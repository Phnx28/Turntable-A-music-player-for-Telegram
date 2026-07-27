"""Entrypoint that binds the address chosen in Settings → Network.

Uvicorn binds its socket before the app exists, so the saved preference has to be
read here, ahead of the server. Launch with `uv run python run.py`.

Precedence: --host flag > BIND_HOST env var > saved setting > 127.0.0.1 (safe default).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import uvicorn

from core import BIND_HOSTS, Database

ROOT = Path(__file__).resolve().parent
DEFAULT_HOST = "127.0.0.1"


def saved_bind_host() -> str:
    """Read the persisted bind host without starting the full application."""
    data_directory = Path(os.environ.get("DATA_DIR", ROOT / "data"))
    database_path = data_directory / "library.sqlite3"
    if not database_path.is_file():
        return DEFAULT_HOST
    database = Database(database_path)
    try:
        host = str(database.get_settings().get("bindHost") or DEFAULT_HOST)
    finally:
        database.close()
    return host if host in BIND_HOSTS else DEFAULT_HOST


def resolve_host(explicit: str | None) -> str:
    for candidate in (explicit, os.environ.get("BIND_HOST")):
        if candidate:
            if candidate not in BIND_HOSTS:
                sys.exit(f"Invalid host {candidate!r}: expected one of {', '.join(sorted(BIND_HOSTS))}")
            return candidate
    return saved_bind_host()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Telegram Turntable")
    parser.add_argument("--host", choices=sorted(BIND_HOSTS), help="Override the saved bind address")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    parser.add_argument("--reload", action="store_true", help="Reload on source changes")
    arguments = parser.parse_args()

    host = resolve_host(arguments.host)

    # The app reads these to tell the UI which address is live, so Settings can
    # show "restart required" when the saved choice differs from reality.
    os.environ["TURNTABLE_ACTIVE_HOST"] = host
    os.environ["TURNTABLE_MANAGED"] = "1"

    if host == "0.0.0.0":
        print("\n  Turntable is listening on ALL network interfaces (0.0.0.0).")
        print("  Anyone who can reach this machine can use it.")
        print("  Set a password in Settings → Network if you have not already.\n")
    else:
        print(f"\n  Turntable is listening on {host}:{arguments.port} (this machine only).\n")

    uvicorn.run(
        "app:app",
        host=host,
        port=arguments.port,
        reload=arguments.reload,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


if __name__ == "__main__":
    main()
