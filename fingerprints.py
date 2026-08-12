"""Chromaprint fingerprinting via the fpcalc command (Phase F1).

fpcalc is the command-line front end of Chromaprint, the acoustic fingerprint
library behind AcoustID. The service degrades gracefully: the app must never
crash or refuse to start because fpcalc is absent -- automatic enrichment just
stays conservative (the resolver never auto-applies without a fingerprint).
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

FPCALC_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class FingerprintResult:
    fingerprint: str
    duration: float
    algorithm_version: int | None = None


class FingerprintError(RuntimeError):
    """fpcalc ran but produced nothing usable."""


class FingerprintService:
    def __init__(self, fpcalc_path: str = "fpcalc", timeout: float = FPCALC_TIMEOUT_SECONDS):
        self.fpcalc_path = fpcalc_path
        self.timeout = timeout
        self._available: bool | None = None

    def available(self) -> bool:
        """Cached availability probe: the command exists and is executable."""
        if self._available is None:
            resolved = shutil.which(self.fpcalc_path)
            self._available = resolved is not None
            if resolved:
                self.fpcalc_path = resolved
        return self._available

    async def fingerprint_track(self, path: Path) -> FingerprintResult:
        """Fingerprint one local audio file. Raises FingerprintError on any failure."""
        if not self.available():
            raise FingerprintError("fpcalc is not installed; fingerprinting unavailable")
        try:
            process = await asyncio.create_subprocess_exec(
                self.fpcalc_path,
                "-json",
                str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise FingerprintError(f"Could not run fpcalc: {error}") from error
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError as error:
            process.kill()
            await process.communicate()
            raise FingerprintError("fpcalc timed out") from error
        if process.returncode:
            detail = stderr.decode("utf-8", "replace").strip() or "no error output"
            raise FingerprintError(f"fpcalc exited {process.returncode}: {detail}")
        try:
            payload = json.loads(stdout.decode("utf-8", "replace"))
            fingerprint = str(payload["fingerprint"] or "")
            duration = float(payload.get("duration") or 0.0)
        except (ValueError, KeyError, TypeError) as error:
            raise FingerprintError("fpcalc returned an unreadable fingerprint") from error
        if not fingerprint or duration <= 0:
            raise FingerprintError("fpcalc returned an empty fingerprint")
        return FingerprintResult(fingerprint=fingerprint, duration=duration)
