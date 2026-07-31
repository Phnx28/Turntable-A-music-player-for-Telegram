import json
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - the suite must still run without a browser
    sync_playwright = None

ROOT = Path(__file__).resolve().parent.parent

SOURCES = [
    {"chatId": "-1001", "kind": "channel", "title": "Hyperdub", "username": "hyperdub", "selected": True,
     "sortOrder": 0, "lastPostAt": 1753800000, "trackCount": 412, "lastMessageId": 9001,
     "lastSyncedAt": 1753830000, "syncError": None, "pinnedAt": 1753000000},
    {"chatId": "-1003", "kind": "private", "title": "ambient dumps", "username": None, "selected": True,
     "sortOrder": 1, "lastPostAt": 1753600000, "trackCount": 96, "lastMessageId": 220,
     "lastSyncedAt": 1753810000, "syncError": None, "pinnedAt": None},
    {"chatId": "-1004", "kind": "saved", "title": "Saved Messages", "username": None, "selected": True,
     "sortOrder": 2, "lastPostAt": 1753500000, "trackCount": 54, "lastMessageId": 800,
     "lastSyncedAt": 1753700000, "syncError": None, "pinnedAt": None},
    {"chatId": "-1005", "kind": "bot", "title": "@deepcuts_bot", "username": "deepcuts_bot", "selected": True,
     "sortOrder": 3, "lastPostAt": 1753400000, "trackCount": 31, "lastMessageId": 90,
     "lastSyncedAt": 1753600000, "syncError": "Flood wait: retry in 42s", "pinnedAt": None},
    {"chatId": "-1006", "kind": "channel", "title": "Nyege Nyege Tapes", "username": "nyegenyege", "selected": True,
     "sortOrder": 4, "lastPostAt": 1753300000, "trackCount": 0, "lastMessageId": 0,
     "lastSyncedAt": None, "syncError": None, "pinnedAt": None},
]

TITLES = [
    ("Angels", "Burial", "-1001", 391000),
    ("Rival Dealer", "Burial", "-1001", 602000),
    ("Sines", "Kode9 & The Spaceape", "-1001", 258000),
    ("An Empty Bliss Beyond This World", "The Caretaker", "-1003", 225000),
    ("03 - untitled draft mixdown FINAL v2 (do not distribute).mp3", "Unknown artist", "-1004", 764000),
    ("Kabuubi", "Otim Alpha", "-1006", 245000),
]


def _tracks(count=48):
    items = []
    for index in range(count):
        title, artist, chat_id, duration = TITLES[index % len(TITLES)]
        source = next(item for item in SOURCES if item["chatId"] == chat_id)
        items.append({
            "key": f"{chat_id}:{1000 + index}",
            "title": title if index < len(TITLES) else f"{title} (take {index // len(TITLES) + 1})",
            "artist": artist,
            "durationMs": duration,
            "sentAt": 1753800000 - index * 3600,
            "artworkVersion": f"v{index}",
            "liked": index % 5 == 1,
            "source": {"chatId": chat_id, "title": source["title"], "kind": source["kind"], "selected": True},
        })
    return items


class Handler(SimpleHTTPRequestHandler):
    # index.html asks for /assets/style.css because app.py:299 mounts /assets -> static/.
    # Without this rewrite every stylesheet 404s and every geometry assertion measures
    # unstyled markup, which is how you get a green test that proves nothing.
    def translate_path(self, path):
        if path.startswith("/assets/"):
            path = path[len("/assets"):]
        return super().translate_path(path)

    def log_message(self, *args):
        pass


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class LayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(ROOT / "static")))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()

    def _stub(self, route):
        # urlsplit rather than hand-rolled splitting: track keys contain ':' and paths carry
        # query strings, and both break naive parsing.
        path = urlsplit(route.request.url).path
        if path.endswith("/cover") or path.endswith("/avatar"):
            # A 404 here is not neutral: a missing cover triggers the img error handler, which
            # replaces the element and changes the geometry being measured.
            return route.fulfill(status=200, content_type="image/svg+xml", body=(
                '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300">'
                '<rect width="300" height="300" fill="#8c2f24"/></svg>'))
        if path.endswith("/audio"):
            return route.fulfill(status=200, content_type="audio/wav", body=b"")
        if path == "/api/auth/status":
            return route.fulfill(status=200, content_type="application/json",
                                 body='{"passwordEnabled": true, "authenticated": true}')
        if path == "/api/status":
            return route.fulfill(status=200, content_type="application/json",
                                 body='{"unlocked": true, "telegram": {"linked": true, "userId": 777, "displayName": "test"}, "startupError": null}')
        if path == "/api/sources":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(SOURCES))
        if path == "/api/library/stats":
            return route.fulfill(status=200, content_type="application/json", body='{"likedCount": 27}')
        if path == "/api/cache/status":
            # "files", not "count". The producer returns bytes, files and states.
            return route.fulfill(status=200, content_type="application/json",
                                 body='{"bytes": 184320000, "files": 41, "states": {}}')
        if path == "/api/tracks":
            items = _tracks()
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"items": items, "offset": 0, "total": len(items)}))
        if path == "/api/settings":
            return route.fulfill(status=200, content_type="application/json",
                                 body='{"musicbrainzContact": "", "coverQuality": "1200", "prefetchCount": 1, "bindHost": "127.0.0.1"}')
        if path == "/api/network":
            return route.fulfill(status=200, content_type="application/json",
                                 body='{"bindHost": "127.0.0.1", "activeHost": "127.0.0.1", "managed": false, "inDocker": false}')
        if path.endswith("/metadata/search"):
            # metadata_candidates is a bare list, not {"candidates": [...]}.
            return route.fulfill(status=200, content_type="application/json", body="[]")
        if path.startswith("/api/jobs/"):
            # Job.public() exposes processed, found, state and result.
            return route.fulfill(status=200, content_type="application/json",
                                 body='{"jobId": "job-1", "kind": "sync", "chatId": "-1001", "mode": "incremental", "state": "complete", "processed": 48, "found": 6, "error": null, "result": null}')
        return route.fulfill(status=200, content_type="application/json", body="{}")

    def page(self, width, height):
        page = self.browser.new_page(viewport={"width": width, "height": height})
        page.route("**/api/**", self._stub)
        page.goto(f"http://127.0.0.1:{self.port}/index.html", wait_until="load")
        page.wait_for_timeout(200)
        self.addCleanup(page.close)
        return page

    def open_now_panel(self, page):
        page.evaluate("""() => {
          document.getElementById('app-shell').hidden = false;
          document.getElementById('lock-view').hidden = true;
          document.getElementById('telegram-view').hidden = true;
          document.getElementById('now-panel').hidden = false;
        }""")
        page.wait_for_timeout(400)

    def test_now_playing_does_not_cover_the_transport_on_a_phone(self):
        page = self.page(390, 844)
        self.open_now_panel(page)
        hit = page.evaluate("""() => {
          const button = document.getElementById('play');
          const box = button.getBoundingClientRect();
          const target = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
          return target === button || button.contains(target) ? 'play' : target.tagName + '#' + target.id;
        }""")
        self.assertEqual("play", hit, "the Now Playing panel covers the play button, so playback is unreachable")

        # Geometry too, not just the hit test: a future z-index change must not silently re-break
        # this while elementFromPoint still happens to pass.
        panel_bottom, player_top = page.evaluate("""() => [
          document.getElementById('now-panel').getBoundingClientRect().bottom,
          document.getElementById('player').getBoundingClientRect().top,
        ]""")
        self.assertLessEqual(panel_bottom, player_top + 1, "the panel overlaps the player box")

    def test_collapsed_rail_does_not_overflow_horizontally(self):
        page = self.page(1440, 900)
        page.evaluate("() => document.querySelector('.app-shell').classList.add('sidebar-collapsed')")
        page.wait_for_timeout(120)
        client, scroll = page.evaluate("""() => {
          const nav = document.getElementById('source-list').closest('nav');
          return [nav.clientWidth, nav.scrollWidth];
        }""")
        self.assertEqual(client, scroll, "collapsed rail overflows horizontally, which spawns a scrollbar with arrows")

    def test_metadata_dialog_buttons_are_not_stretched(self):
        page = self.page(1440, 900)
        page.evaluate("() => document.getElementById('metadata-dialog').showModal()")
        page.wait_for_timeout(120)
        display, heights = page.evaluate("""() => [
          getComputedStyle(document.querySelector('.metadata-form .inline-choice')).display,
          [...document.querySelectorAll('#metadata-form .form-actions .button')].map((b) => Math.round(b.getBoundingClientRect().height)),
        ]""")
        self.assertIn(display, {"flex", "inline-flex"})
        self.assertTrue(all(h == 40 for h in heights), f"buttons stretched to {heights} instead of 40px")

    def test_failed_metadata_lookup_clears_its_skeleton(self):
        page = self.page(1440, 900)
        page.route("**/metadata/search", lambda route: route.fulfill(
            status=429, content_type="application/json",
            body='{"error": {"message": "Rate limited", "retryable": true}}'))
        page.evaluate("() => document.getElementById('metadata-dialog').showModal()")
        page.click("#fetch-metadata")
        page.wait_for_function("() => !document.getElementById('fetch-metadata').disabled")
        hidden, markup = page.evaluate("""() => [
          document.getElementById('candidate-section').hidden,
          document.getElementById('candidate-list').innerHTML,
        ]""")
        self.assertTrue(hidden, "candidate section stayed open after a failed lookup")
        self.assertNotIn("list-skeleton", markup, "the loading skeleton outlived the failure")
