"""Per-category storage measurement for the Storage settings surface.

Phase B7 measures the six generated-data categories (audio cache, tagged downloads,
artwork, Telegram thumbnails, avatars, database) so growth is visible and future
deletion policies have a budget to enforce. The walk is deliberately synchronous --
callers run it via asyncio.to_thread, since summing directory sizes is blocking work.
"""

from __future__ import annotations

from pathlib import Path


def measure_path(path: Path) -> dict[str, int]:
    """Total bytes and file count under *path*; a plain file counts as itself.

    Missing paths and unreadable entries report zero rather than raising, so a half-
    deleted category never breaks the summary.
    """
    try:
        if path.is_file():
            return {"bytes": path.stat().st_size, "files": 1}
    except OSError:
        return {"bytes": 0, "files": 0}
    total_bytes = 0
    total_files = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total_bytes += entry.stat().st_size
                    total_files += 1
            except OSError:
                continue
    except OSError:
        pass
    return {"bytes": total_bytes, "files": total_files}


def storage_summary(paths: list[tuple[str, Path]]) -> dict[str, object]:
    """Per-category measurement keyed by stable category names.

    The category names are the API contract the Storage settings page will consume;
    missing categories report zero bytes/files instead of disappearing.
    """
    return {"categories": {name: measure_path(path) for name, path in paths}}
