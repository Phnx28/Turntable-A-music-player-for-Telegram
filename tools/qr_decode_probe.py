"""Decoder-level verification for the Telegram login QR renderer.

Rasterizes render_qr_svg() at every size the app really renders it (the .qr-inset content box
runs ~204-224px, plus a safety band around it), samples every module centre against segno's
matrix, and decodes each raster with OpenCV's QRCodeDetector. This is the "real-scanner test"
the QR_MODULE_RADIUS comment demands before the radius may be raised: at .34 the detector lost
decodes at 180px and 216px while .28 and the square canonical renderer decoded every size.

Usage:

    uv pip install --python .venv/bin/python opencv-python-headless  # cv2 (probe only, not a dep)
    .venv/bin/python tools/qr_decode_probe.py                       # playwright must be importable

Payloads are randomly generated fakes; never feed this a real token.
"""

from __future__ import annotations

import base64
import secrets
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import cv2
    import numpy as np
    from playwright.sync_api import sync_playwright
except ImportError as error:  # pragma: no cover - probe-only dependencies
    raise SystemExit(
        "qr_decode_probe needs opencv-python-headless and playwright; install opencv with "
        "uv pip install --python .venv/bin/python opencv-python-headless. Error: "
        + repr(error)
    ) from error

import telegram_service
from telegram_service import QR_QUIET_MODULES, render_qr_svg

# 176-252 covers every real .qr-inset render; 150-170 are the margin-below band.
SIZES = (150, 160, 170, 176, 180, 184, 190, 200, 204, 210, 216, 224, 232, 252)
PAYLOAD_COUNT = 6


def rasterize(browser, svg: str, px: int) -> np.ndarray:
    sized = svg.replace('<svg xmlns=', f'<svg width="{px}" height="{px}" xmlns=', 1)
    b64 = base64.b64encode(sized.encode()).decode()
    page = browser.new_page()
    try:
        page.set_content(
            f'<canvas id="c" width="{px}" height="{px}"></canvas>'
            f'<script>const i=new Image();i.onload=()=>{{'
            f'const x=document.getElementById("c").getContext("2d");'
            f'x.drawImage(i,0,0,{px},{px});'
            f'window.__data=Array.from(x.getImageData(0,0,{px},{px}).data);}};'
            f'i.src="data:image/svg+xml;base64,{b64}";</script>'
        )
        page.wait_for_function(
            "() => Array.isArray(window.__data) && window.__data.length === "
            + str(px * px * 4)
        )
        rgba = np.frombuffer(
            np.array(page.evaluate("() => window.__data"), dtype=np.uint8),
            dtype=np.uint8,
        ).reshape(px, px, 4)
        return cv2.cvtColor(rgba[:, :, :3], cv2.COLOR_RGB2GRAY)
    finally:
        page.close()


def luminance_checks(gray: np.ndarray, matrix, px: int) -> tuple[int, int, int, int]:
    extent = len(matrix) + QR_QUIET_MODULES * 2
    scale = px / extent

    def dark(value) -> bool:
        return value < 128

    def sample(col, row):
        x = int((col + 0.5 + QR_QUIET_MODULES) * scale)
        y = int((row + 0.5 + QR_QUIET_MODULES) * scale)
        return gray[y, x]

    size = len(matrix)
    mismatches = sum(
        dark(sample(col, row)) != bool(matrix[row][col])
        for row in range(size)
        for col in range(size)
    )
    seams = 0
    for row in range(size):
        for col in range(size - 1):
            if matrix[row][col] and matrix[row][col + 1]:
                x = int((col + 1 + QR_QUIET_MODULES) * scale)
                y = int((row + 0.5 + QR_QUIET_MODULES) * scale)
                seams += not dark(gray[y, x])
    for row in range(size - 1):
        for col in range(size):
            if matrix[row][col] and matrix[row + 1][col]:
                x = int((col + 0.5 + QR_QUIET_MODULES) * scale)
                y = int((row + 1 + QR_QUIET_MODULES) * scale)
                seams += not dark(gray[y, x])
    margin = int(QR_QUIET_MODULES * scale)
    quiet_bad = int((gray[:, :margin] < 200).sum()) + int((gray[:, -margin:] < 200).sum())
    quiet_bad += int((gray[:margin, :] < 200).sum()) + int((gray[-margin:, :] < 200).sum())
    finder_bad = 0
    for r0, c0 in ((0, 0), (0, size - 7), (size - 7, 0)):
        for row in range(r0, r0 + 7):
            for col in range(c0, c0 + 7):
                finder_bad += bool(matrix[row][col] and not dark(sample(col, row)))
    return mismatches, seams, int(quiet_bad), finder_bad


def main() -> None:
    payloads = [
        "tg://login?token="
        + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
        for _ in range(PAYLOAD_COUNT)
    ]
    failures = 0
    detector = cv2.QRCodeDetector()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for payload in payloads:
            import segno

            matrix = segno.make(payload).matrix
            svg = render_qr_svg(payload)
            for px in SIZES:
                gray = rasterize(browser, svg, px)
                mismatches, seams, quiet_bad, finder_bad = luminance_checks(
                    gray, matrix, px
                )
                assert mismatches == 0, f"{px}px: {mismatches} module centres wrong"
                assert seams == 0, f"{px}px: {seams} adjacent dark modules gapped"
                assert quiet_bad == 0, f"{px}px: {quiet_bad} quiet-zone pixels impure"
                assert finder_bad == 0, f"{px}px: {finder_bad} finder modules notched"
                data, _, _ = detector.detectAndDecode(gray)
                if data != payload:
                    failures += 1
                    print(f"{px}px: DECODE FAILED")
        browser.close()
    total = len(payloads) * len(SIZES)
    print(
        f"radius {telegram_service.QR_MODULE_RADIUS}: {total - failures}/{total} decodes "
        f"passed across {len(SIZES)} sizes x {len(payloads)} payloads"
    )
    if failures:
        raise SystemExit(f"{failures} decode failures at the current radius")


if __name__ == "__main__":
    main()
