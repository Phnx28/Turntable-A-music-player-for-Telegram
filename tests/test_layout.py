import json
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

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


def _rel_lum(rgb):
    """Relative luminance (WCAG) for an [r, g, b] byte triple."""
    def chan(v):
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


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

    def test_details_tab_shows_everything_already_indexed(self):
        page = self.page(1440, 900)
        self.open_now_panel(page)
        page.evaluate("""() => {
          document.getElementById('details-pane').hidden = false;
          document.getElementById('track-details').innerHTML = '';
        }""")
        # Drive the real render path rather than asserting on hand-written markup.
        page.evaluate("""() => window.__renderDetailsForTest({
          key: '-1001:1000', source: { title: 'Hyperdub' },
          metadata: { album: 'Rival Dealer', albumArtist: 'Burial', genre: 'Bass',
                      year: 2013, trackNumber: 2, discNumber: 1 },
          file: { name: 'rival-dealer.mp3', mimeType: 'audio/mpeg', size: 14_680_064 },
          durationMs: 602_000, sentAt: 1753800000,
        })""")
        labels = page.evaluate("() => [...document.querySelectorAll('#track-details dt')].map((d) => d.textContent)")
        for expected in ["Duration", "Posted", "Track", "Disc", "Format"]:
            self.assertIn(expected, labels, f"{expected} is indexed but not shown: {labels}")
        values = page.evaluate("() => [...document.querySelectorAll('#track-details dd')].map((d) => d.textContent)")
        self.assertIn("10:02", values, f"duration not formatted: {values}")
        self.assertIn("MPEG", values, f"audio mime type not formatted: {values}")
        self.assertIn("14.0 MB", values, f"file size not formatted: {values}")

        # Actions must be reachable without scrolling the pane: DOCUMENT_POSITION_FOLLOWING (4)
        # means the list comes after the actions.
        position = page.evaluate("""() => document.querySelector('.detail-actions')
          .compareDocumentPosition(document.getElementById('track-details'))""")
        self.assertEqual(4, position & 4, "the action buttons must precede the detail list")

        page.evaluate("""() => window.__renderDetailsForTest({
          key: '-1001:1001', source: { title: 'Hyperdub' },
          metadata: {}, file: { name: 'untitled.mp3', size: 0 },
        })""")
        sparse = page.evaluate("""() => ({
          labels: [...document.querySelectorAll('#track-details dt')].map((d) => d.textContent),
          values: [...document.querySelectorAll('#track-details dd')].map((d) => d.textContent),
        })""")
        for absent in ["Album", "Year", "Duration", "Posted", "Track", "Disc", "Format", "Size"]:
            self.assertNotIn(absent, sparse["labels"], f"sparse track rendered absent row: {sparse}")
        self.assertNotIn("0", sparse["values"], f"sparse track rendered a zero value: {sparse}")
        self.assertTrue(all(value for value in sparse["values"]), f"sparse track rendered an empty dd: {sparse}")

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

    def test_phones_can_filter_the_open_playlist(self):
        page = self.page(390, 844)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_timeout(150)
        visible = page.evaluate("""() => {
          const shown = (selector) => {
            const element = document.querySelector(selector);
            if (!element) return "missing";
            const style = getComputedStyle(element);
            return style.display !== "none" && style.visibility !== "hidden";
          };
          return { filter: shown(".search-control"), count: shown("#library-summary") };
        }""")
        self.assertIs(True, visible["filter"], "the playlist filter is hidden on a phone, so narrowing is impossible")
        self.assertIs(True, visible["count"], "the track count is hidden on a phone")

    def test_narrow_rows_give_their_space_to_titles(self):
        page = self.page(320, 720)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_selector(".track-row:not(.track-placeholder)")
        shape = page.evaluate("""() => {
          const row = document.querySelector('.track-row:not(.track-placeholder)');
          const hidden = (selector) => {
            const el = row.querySelector(selector);
            return !el || getComputedStyle(el).display === 'none';
          };
          return {
            rowWidth: Math.round(row.getBoundingClientRect().width),
            mainWidth: Math.round(row.querySelector('.track-main').getBoundingClientRect().width),
            copyWidth: Math.round(row.querySelector('.track-copy').getBoundingClientRect().width),
            ordinalWidth: Math.round(row.querySelector('.track-ordinal').getBoundingClientRect().width),
            visibleCells: [...row.children]
              .filter((child) => getComputedStyle(child).display !== 'none')
              .map((child) => child.classList.contains('row-menu') ? 'row-menu' : child.classList[0]),
            postedHidden: hidden('.track-posted'),
            durationHidden: hidden('.track-duration'),
            likeHidden: hidden('.track-row-actions'),
            menuOpacity: getComputedStyle(row.querySelector('.row-menu')).opacity,
          };
        }""")
        self.assertEqual(["track-ordinal", "track-main", "row-menu"], shape["visibleCells"], shape)
        self.assertTrue(shape["postedHidden"], "the date should yield before title space at 320px")
        self.assertTrue(shape["durationHidden"], "duration should move to the row sheet at 320px")
        self.assertTrue(shape["likeHidden"], "the like button should move to the row sheet at 320px")
        self.assertEqual("1", shape["menuOpacity"], "the 320px row sheet trigger must remain reachable")
        self.assertLessEqual(shape["ordinalWidth"], 30, "the ordinal should shrink to ~3ch")
        self.assertGreater(shape["mainWidth"] / shape["rowWidth"], 0.7,
                           f"the main column is still starved: {shape}")
        # Text copy gets the main column after the measured 40px artwork and 12px gap;
        # the 2px tolerance covers integer rounding of the browser's layout rectangles.
        self.assertGreaterEqual(shape["copyWidth"], shape["mainWidth"] - 54,
                                f"text copy lost more than the artwork+gap budget: {shape}")
        page.click(".track-row:not(.track-placeholder) .row-menu")
        page.wait_for_selector("#context-menu:not([hidden])")

    def test_row_sheet_opens_with_reachable_targets(self):
        page = self.page(390, 844)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_selector(".track-row:not(.track-placeholder)")
        page.click(".track-row:not(.track-placeholder) .row-menu")
        page.wait_for_selector("#context-menu:not([hidden])")
        page.wait_for_timeout(150)
        shape = page.evaluate("""() => {
          const menu = document.getElementById('context-menu');
          const player = document.getElementById('player');
          const row = document.querySelector('.track-row:not(.track-placeholder)');
          return {
            sizes: [...menu.querySelectorAll('button')]
              .map((button) => Math.round(button.getBoundingClientRect().height)),
            labels: [...menu.querySelectorAll('button')].map((button) => button.textContent),
            visibleCells: [...row.children]
              .filter((child) => getComputedStyle(child).display !== 'none')
              .map((child) => child.classList.contains('row-menu') ? 'row-menu' : child.classList[0]),
            menuOpacity: getComputedStyle(row.querySelector('.row-menu')).opacity,
            menuTop: menu.getBoundingClientRect().top,
            menuBottom: menu.getBoundingClientRect().bottom,
            playerTop: player.getBoundingClientRect().top,
          };
        }""")
        self.assertEqual(["track-ordinal", "track-main", "track-posted", "row-menu"], shape["visibleCells"], shape)
        self.assertTrue(shape["sizes"], "the row menu opened empty")
        self.assertTrue(all(height >= 44 for height in shape["sizes"]),
                        f"sheet targets under 44px: {shape}")
        like_labels = [label for label in shape["labels"] if label in {"Like", "Unlike"}]
        self.assertEqual(1, len(like_labels), f"the narrow row menu must expose exactly one Like/Unlike branch: {shape}")
        self.assertTrue(any(label.startswith("Duration ") for label in shape["labels"]),
                        f"the narrow row menu lacks formatted duration: {shape}")
        self.assertEqual("1", shape["menuOpacity"],
                         f"the row sheet trigger must remain visible on a phone: {shape}")
        self.assertGreaterEqual(shape["menuTop"], 0, f"sheet starts above the viewport: {shape}")
        self.assertLessEqual(shape["menuBottom"], shape["playerTop"] + 1,
                             f"sheet overlaps the player: {shape}")

    def test_narrow_sheet_like_propagates_canonical_state_and_rolls_back(self):
        page = self.page(390, 844)
        patches = []
        tracks = {track["key"]: {**track, "metadata": {"title": track["title"], "artist": track["artist"]},
                                  "file": {"name": f"{track['title']}.mp3", "mimeType": "audio/mpeg"}}
                  for track in _tracks(2)}

        def detail_and_like(route):
            path = urlsplit(route.request.url).path
            if path.endswith("1000/like"):
                patches.append(route)
                return
            if path.endswith("1000") or path.endswith("1001"):
                key = "-1001:1000" if path.endswith("1000") else "-1001:1001"
                return route.fulfill(status=200, content_type="application/json", body=json.dumps(tracks[key]))
            return route.fallback()

        # Playing the row is the real getTrack consumer: its detail response is a distinct
        # object from the library row, and is retained for the later play-back-to-this-row path.
        page.route("**/api/**", detail_and_like)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_selector(".track-row:not(.track-placeholder)")
        rows = page.locator(".track-row:not(.track-placeholder)")
        rows.nth(0).locator(".track-main").click()
        page.wait_for_function("() => document.getElementById('player-title').textContent === 'Angels'")

        def close_fixture_audio_error():
            page.wait_for_timeout(150)
            if page.evaluate("() => document.getElementById('error-dialog').open"):
                page.locator("#error-dialog [data-close='error-dialog']").click()

        def click_sheet_like():
            rows.nth(0).locator(".row-menu").click()
            page.wait_for_selector("#context-menu:not([hidden])")
            page.get_by_role("menuitem", name="Like", exact=True).click()

        close_fixture_audio_error()
        click_sheet_like()
        page.wait_for_function("() => document.getElementById('like-current').getAttribute('aria-pressed') === 'true'")
        optimistic = page.evaluate("""() => ({
          row: document.querySelector('.track-row:not(.track-placeholder) .row-like').getAttribute('aria-pressed'),
          current: document.getElementById('like-current').getAttribute('aria-pressed'),
          count: document.getElementById('liked-count').textContent,
        })""")
        self.assertEqual({"row": "true", "current": "true", "count": "28"}, optimistic)
        self.assertEqual(1, len(patches), "the sheet Like action did not reach PATCH")

        # The server's canonical answer deliberately rejects the requested like.
        patches.pop(0).fulfill(status=200, content_type="application/json", body='{"liked": false}')
        page.wait_for_timeout(200)
        canonical = page.evaluate("""() => ({
          row: document.querySelector('.track-row:not(.track-placeholder) .row-like').getAttribute('aria-pressed'),
          current: document.getElementById('like-current').getAttribute('aria-pressed'),
          count: document.getElementById('liked-count').textContent,
        })""")
        self.assertEqual({"row": "false", "current": "false", "count": "27"}, canonical)

        # Re-consume the cached detail through the real row playback path; canonical success must
        # not leave the distinct detail copy stale and revive the optimistic liked state.
        rows.nth(1).locator(".track-main").click()
        page.wait_for_function("() => document.getElementById('player-title').textContent === 'Rival Dealer'")
        close_fixture_audio_error()
        rows.nth(0).locator(".track-main").click()
        page.wait_for_function("() => document.getElementById('player-title').textContent === 'Angels'")
        close_fixture_audio_error()
        self.assertEqual("false", page.locator("#like-current").get_attribute("aria-pressed"))

        click_sheet_like()
        page.wait_for_function("() => document.getElementById('like-current').getAttribute('aria-pressed') === 'true'")
        self.assertEqual(1, len(patches), "the rollback PATCH was not captured")
        patches.pop(0).fulfill(status=500, content_type="application/json",
                               body='{"error": {"message": "like failed", "retryable": true}}')
        page.wait_for_function("() => document.getElementById('like-current').getAttribute('aria-pressed') === 'false'")
        rolled_back = page.evaluate("""() => ({
          row: document.querySelector('.track-row:not(.track-placeholder) .row-like').getAttribute('aria-pressed'),
          current: document.getElementById('like-current').getAttribute('aria-pressed'),
          count: document.getElementById('liked-count').textContent,
          errorOpen: document.getElementById('error-dialog').open,
        })""")
        self.assertEqual({"row": "false", "current": "false", "count": "27", "errorOpen": True}, rolled_back)

    def test_narrow_sheet_like_updates_current_created_while_patch_is_pending(self):
        page = self.page(390, 844)
        patches = []
        track = {**_tracks(1)[0], "metadata": {"title": "Angels", "artist": "Burial"},
                 "file": {"name": "angels.mp3", "mimeType": "audio/mpeg"}}

        def detail_and_like(route):
            path = urlsplit(route.request.url).path
            if path.endswith("1000/like"):
                patches.append(route)
                return
            if path.endswith("1000"):
                return route.fulfill(status=200, content_type="application/json", body=json.dumps(track))
            return route.fallback()

        page.route("**/api/**", detail_and_like)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_selector(".track-row:not(.track-placeholder)")
        row = page.locator(".track-row:not(.track-placeholder)").nth(0)
        row.locator(".row-menu").click()
        page.wait_for_selector("#context-menu:not([hidden])")
        page.get_by_role("menuitem", name="Like", exact=True).click()
        page.wait_for_function("() => document.querySelector('.track-row:not(.track-placeholder) .row-like').getAttribute('aria-pressed') === 'true'")
        self.assertEqual(1, len(patches), "the optimistic Like did not remain pending")

        # This creates the current clone after toggleRowLike captured its initial representations.
        row.locator(".track-main").click()
        page.wait_for_function("() => document.getElementById('player-title').textContent === 'Angels'")
        pending = page.evaluate("""() => {
          const rowLike = document.querySelector('.track-row:not(.track-placeholder) .row-like');
          const current = document.getElementById('like-current');
          return {
            rowPressed: rowLike.getAttribute('aria-pressed'),
            rowActive: rowLike.classList.contains('active'),
            rowIcon: rowLike.querySelector('use').getAttribute('href'),
            currentPressed: current.getAttribute('aria-pressed'),
            currentActive: current.classList.contains('active'),
            currentIcon: current.querySelector('use').getAttribute('href'),
            count: document.getElementById('liked-count').textContent,
          };
        }""")
        self.assertEqual({
          "rowPressed": "true", "rowActive": True, "rowIcon": "#i-heart-filled",
          "currentPressed": "true", "currentActive": True, "currentIcon": "#i-heart-filled",
          "count": "28",
        }, pending)

        patches[0].fulfill(status=200, content_type="application/json", body='{"liked": true}')
        page.wait_for_function("() => document.getElementById('like-current').getAttribute('aria-pressed') === 'true'")
        settled = page.evaluate("""() => ({
          rowPressed: document.querySelector('.track-row:not(.track-placeholder) .row-like').getAttribute('aria-pressed'),
          rowActive: document.querySelector('.track-row:not(.track-placeholder) .row-like').classList.contains('active'),
          rowIcon: document.querySelector('.track-row:not(.track-placeholder) .row-like use').getAttribute('href'),
          currentPressed: document.getElementById('like-current').getAttribute('aria-pressed'),
          currentActive: document.getElementById('like-current').classList.contains('active'),
          currentIcon: document.querySelector('#like-current use').getAttribute('href'),
          count: document.getElementById('liked-count').textContent,
        })""")
        self.assertEqual({
          "rowPressed": "true", "rowActive": True, "rowIcon": "#i-heart-filled",
          "currentPressed": "true", "currentActive": True, "currentIcon": "#i-heart-filled",
          "count": "28",
        }, settled)

    def test_pending_row_like_then_player_heart_toggles_from_authoritative_desired_state(self):
        page = self.page(390, 844)
        patches = []
        track = {**_tracks(1)[0], "metadata": {"title": "Angels", "artist": "Burial"},
                 "file": {"name": "angels.mp3", "mimeType": "audio/mpeg"}}

        def detail_and_like(route):
            path = urlsplit(route.request.url).path
            if path.endswith("1000/like"):
                patches.append(route)
                return
            if path.endswith("1000"):
                return route.fulfill(status=200, content_type="application/json", body=json.dumps(track))
            return route.fallback()

        page.route("**/api/**", detail_and_like)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_selector(".track-row:not(.track-placeholder)")
        row = page.locator(".track-row:not(.track-placeholder)").nth(0)

        row.locator(".row-menu").click()
        page.wait_for_selector("#context-menu:not([hidden])")
        page.get_by_role("menuitem", name="Like", exact=True).click()
        page.wait_for_function("() => document.querySelector('.track-row:not(.track-placeholder) .row-like').getAttribute('aria-pressed') === 'true'")
        self.assertEqual(1, len(patches), "the row Like request did not remain pending")

        row.locator(".track-main").click()
        page.wait_for_function("() => document.getElementById('player-title').textContent === 'Angels'")
        page.wait_for_timeout(150)
        if page.evaluate("() => document.getElementById('error-dialog').open"):
            page.locator("#error-dialog [data-close='error-dialog']").click()
        page.click("#like-current")
        for _ in range(20):
            if len(patches) == 2:
                break
            page.wait_for_timeout(25)
        self.assertEqual(2, len(patches), "the player heart did not issue the intended second operation")
        self.assertEqual({"liked": True}, json.loads(patches[0].request.post_data))
        self.assertEqual({"liked": False}, json.loads(patches[1].request.post_data),
                         "the player heart must invert the pending desired state")

        # The older success is ignored by the latest-per-key guard; the newer failure rolls back
        # to the first operation's desired state rather than the stale detail/current clone.
        patches[0].fulfill(status=200, content_type="application/json", body='{"liked": true}')
        page.wait_for_timeout(80)
        patches[1].fulfill(status=500, content_type="application/json",
                           body='{"error": {"message": "like failed", "retryable": true}}')
        page.wait_for_function("() => document.getElementById('like-current').getAttribute('aria-pressed') === 'true'")
        settled = page.evaluate("""() => ({
          row: document.querySelector('.track-row:not(.track-placeholder) .row-like').getAttribute('aria-pressed'),
          current: document.getElementById('like-current').getAttribute('aria-pressed'),
          count: document.getElementById('liked-count').textContent,
        })""")
        self.assertEqual({"row": "true", "current": "true", "count": "28"}, settled)

        if page.evaluate("() => document.getElementById('error-dialog').open"):
            page.locator("#error-dialog [data-close='error-dialog']").click()
        page.locator(".track-row:not(.track-placeholder) .row-menu").nth(0).click()
        page.wait_for_selector("#context-menu:not([hidden])")
        self.assertIn("Unlike", page.evaluate("() => [...document.querySelectorAll('#context-menu button')].map((button) => button.textContent)"))

    def test_two_same_key_like_failures_roll_back_to_the_canonical_baseline(self):
        page = self.page(390, 844)
        patches = []

        def pending_like(route):
            if urlsplit(route.request.url).path.endswith("1000/like"):
                patches.append(route)
                return
            return route.fallback()

        page.route("**/api/**", pending_like)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_selector(".track-row:not(.track-placeholder)")
        row = page.locator(".track-row:not(.track-placeholder)").nth(0)

        for label in ("Like", "Unlike"):
            row.locator(".row-menu").click()
            page.wait_for_selector("#context-menu:not([hidden])")
            page.get_by_role("menuitem", name=label, exact=True).click()
            page.wait_for_timeout(100)

        self.assertEqual(2, len(patches), "both rapid same-key operations must reach PATCH")
        self.assertEqual({"liked": True}, json.loads(patches[0].request.post_data))
        self.assertEqual({"liked": False}, json.loads(patches[1].request.post_data))
        self.assertEqual("false", row.locator(".row-like").get_attribute("aria-pressed"))
        self.assertEqual("27", page.locator("#liked-count").text_content())

        for patch in patches:
            patch.fulfill(status=500, content_type="application/json",
                          body='{"error": {"message": "like failed", "retryable": true}}')
        page.wait_for_function("() => document.querySelector('.track-row:not(.track-placeholder) .row-like').getAttribute('aria-pressed') === 'false'")
        settled = page.evaluate("""() => ({
          row: document.querySelector('.track-row:not(.track-placeholder) .row-like').getAttribute('aria-pressed'),
          current: document.getElementById('like-current').getAttribute('aria-pressed'),
          count: document.getElementById('liked-count').textContent,
        })""")
        self.assertEqual({"row": "false", "current": "false", "count": "27"}, settled)

    def test_latest_row_like_operation_wins_when_patch_responses_arrive_out_of_order(self):
        page = self.page(390, 844)
        patches = []

        def pending_like(route):
            path = urlsplit(route.request.url).path
            if path.endswith("1000/like"):
                patches.append(route)
                return
            return route.fallback()

        page.route("**/api/**", pending_like)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_selector(".track-row:not(.track-placeholder)")
        row = page.locator(".track-row:not(.track-placeholder)").nth(0)

        row.locator(".row-menu").click()
        page.wait_for_selector("#context-menu:not([hidden])")
        page.get_by_role("menuitem", name="Like", exact=True).click()
        page.wait_for_function("() => document.querySelector('.track-row:not(.track-placeholder) .row-like').getAttribute('aria-pressed') === 'true'")
        page.wait_for_timeout(150)
        row.locator(".row-menu").click()
        page.wait_for_selector("#context-menu:not([hidden])")
        page.get_by_role("menuitem", name="Unlike", exact=True).click()
        for _ in range(20):
            if len(patches) == 2:
                break
            page.wait_for_timeout(25)
        self.assertEqual(2, len(patches), "both optimistic inversions must reach PATCH")
        self.assertEqual("false", row.locator(".row-like").get_attribute("aria-pressed"))
        self.assertEqual("27", page.locator("#liked-count").text_content())

        # The newer request wins even when the older response arrives afterward.
        patches[1].fulfill(status=200, content_type="application/json", body='{"liked": false}')
        page.wait_for_timeout(80)
        patches[0].fulfill(status=200, content_type="application/json", body='{"liked": true}')
        page.wait_for_timeout(150)
        settled = page.evaluate("""() => ({
          row: document.querySelector('.track-row:not(.track-placeholder) .row-like').getAttribute('aria-pressed'),
          count: document.getElementById('liked-count').textContent,
        })""")
        self.assertEqual({"row": "false", "count": "27"}, settled)

    def test_narrow_menu_uses_detail_like_when_search_summary_omits_like(self):
        page = self.page(390, 844)
        key = "-1009:77"
        patches = []
        detail = {
            "key": key, "title": "Burial", "artist": "Burial", "liked": True, "durationMs": 242000,
            "metadata": {"title": "Burial", "artist": "Burial"},
            "file": {"name": "burial.mp3", "mimeType": "audio/mpeg"},
            "source": {"chatId": "-1009", "title": "Telegram Vault", "kind": "channel", "selected": False},
        }

        def search_and_detail(route):
            path = urlsplit(route.request.url).path
            if path == "/api/search/telegram":
                return route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "sources": [],
                    # Search summaries can omit liked; the detail response is authoritative here.
                    "tracks": [{key_name: value for key_name, value in detail.items()
                                if key_name not in {"liked", "metadata", "file"}}],
                }))
            if path.endswith("77/like"):
                patches.append(route)
                return
            if path.startswith("/api/tracks/") and path.endswith("77"):
                return route.fulfill(status=200, content_type="application/json", body=json.dumps(detail))
            return route.fallback()

        page.route("**/api/**", search_and_detail)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.fill("#global-search", "burial")
        page.wait_for_selector("[data-global-track]")
        page.evaluate("() => document.querySelector('[data-global-track]').click()")
        page.wait_for_function("() => document.getElementById('player-title').textContent === 'Burial'")
        page.wait_for_timeout(150)
        if page.evaluate("() => document.getElementById('error-dialog').open"):
            page.locator("#error-dialog [data-close='error-dialog']").click()
        page.click("#player-more")
        page.wait_for_selector("#context-menu:not([hidden])")
        labels = page.evaluate("() => [...document.querySelectorAll('#context-menu button')].map((button) => button.textContent)")
        self.assertIn("Unlike", labels, f"detail/current liked state was ignored: {labels}")
        page.get_by_role("menuitem", name="Unlike", exact=True).click()
        page.wait_for_timeout(100)
        self.assertEqual(1, len(patches), "the menu action did not reach PATCH")
        self.assertEqual({"liked": False}, json.loads(patches[0].request.post_data),
                         "the menu payload must invert the authoritative detail/current state")
        self.assertEqual("false", page.locator("#like-current").get_attribute("aria-pressed"))
        self.assertEqual("26", page.locator("#liked-count").text_content())
        patches[0].fulfill(status=200, content_type="application/json", body='{"liked": false}')
        page.wait_for_function("() => document.getElementById('like-current').getAttribute('aria-pressed') === 'false'")

    def test_player_heart_synchronizes_distinct_row_summary_detail_and_current(self):
        page = self.page(390, 844)
        patches = []
        key = "-1001:1000"
        row = _tracks(1)[0]
        summary = {**row, "liked": False, "artworkVersion": "search-copy"}
        detail = {**row, "liked": False, "metadata": {"title": "Angels", "artist": "Burial"},
                  "file": {"name": "angels.mp3", "mimeType": "audio/mpeg"}}

        def distinct_representations(route):
            path = urlsplit(route.request.url).path
            if path == "/api/search/telegram":
                return route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "sources": [], "tracks": [summary],
                }))
            if path.endswith("1000/like"):
                patches.append(route)
                return
            if path.endswith("1000"):
                return route.fulfill(status=200, content_type="application/json", body=json.dumps(detail))
            return route.fallback()

        page.route("**/api/**", distinct_representations)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.fill("#global-search", "angels")
        page.wait_for_selector("[data-global-track]")
        page.press("#global-search", "Escape")
        page.locator(".track-row:not(.track-placeholder) .track-main").nth(0).click()
        page.wait_for_function("() => document.getElementById('player-title').textContent === 'Angels'")
        page.wait_for_timeout(150)
        if page.evaluate("() => document.getElementById('error-dialog').open"):
            page.locator("#error-dialog [data-close='error-dialog']").click()

        page.click("#like-current")
        page.wait_for_function("() => document.getElementById('like-current').getAttribute('aria-pressed') === 'true'")
        page.wait_for_timeout(100)
        self.assertEqual(1, len(patches), "the player heart did not reach PATCH")
        self.assertEqual({"liked": True}, json.loads(patches[0].request.post_data))
        optimistic = page.evaluate("""() => ({
          row: document.querySelector('.track-row:not(.track-placeholder) .row-like').getAttribute('aria-pressed'),
          current: document.getElementById('like-current').getAttribute('aria-pressed'),
          count: document.getElementById('liked-count').textContent,
        })""")
        self.assertEqual({"row": "true", "current": "true", "count": "28"}, optimistic)
        patches.pop(0).fulfill(status=200, content_type="application/json", body='{"liked": true}')
        page.wait_for_timeout(150)

        page.click("#like-current")
        page.wait_for_timeout(100)
        self.assertEqual(1, len(patches), "the second player-heart operation did not reach PATCH")
        self.assertEqual({"liked": False}, json.loads(patches[0].request.post_data))
        patches[0].fulfill(status=500, content_type="application/json",
                           body='{"error": {"message": "like failed", "retryable": true}}')
        page.wait_for_function("() => document.getElementById('like-current').getAttribute('aria-pressed') === 'true'")
        rolled_back = page.evaluate("""() => ({
          row: document.querySelector('.track-row:not(.track-placeholder) .row-like').getAttribute('aria-pressed'),
          current: document.getElementById('like-current').getAttribute('aria-pressed'),
          count: document.getElementById('liked-count').textContent,
        })""")
        self.assertEqual({"row": "true", "current": "true", "count": "28"}, rolled_back)
        if page.evaluate("() => document.getElementById('error-dialog').open"):
            page.locator("#error-dialog [data-close='error-dialog']").click()
        page.locator(".track-row:not(.track-placeholder) .row-menu").nth(0).click()
        page.wait_for_selector("#context-menu:not([hidden])")
        self.assertIn("Unlike", page.evaluate("() => [...document.querySelectorAll('#context-menu button')].map((button) => button.textContent)"))

    def test_320px_library_heading_uses_title_token(self):
        page = self.page(320, 844)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_selector(".track-row:not(.track-placeholder)")
        size = page.evaluate("""() => {
          const heading = document.querySelector('.library-heading h1');
          const expected = document.createElement('span');
          expected.style.fontSize = 'var(--text-title)';
          document.body.append(expected);
          const result = [getComputedStyle(heading).fontSize, getComputedStyle(expected).fontSize];
          expected.remove();
          return result;
        }""")
        self.assertEqual(size[1], size[0], f"320px heading drifted from --text-title: {size}")

    def test_desktop_track_menu_does_not_duplicate_row_controls(self):
        page = self.page(1440, 900)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_selector(".track-row:not(.track-placeholder)")
        row = page.locator(".track-row:not(.track-placeholder)").nth(0)
        rest = page.evaluate("""() => {
          const row = document.querySelector('.track-row:not(.track-placeholder)');
          const graphite = document.createElement('span');
          graphite.style.color = 'var(--graphite)';
          document.body.append(graphite);
          const expected = getComputedStyle(graphite).color;
          graphite.remove();
          return {
            menuOpacity: getComputedStyle(row.querySelector('.row-menu')).opacity,
            menuColor: getComputedStyle(row.querySelector('.row-menu')).color,
            actionsOpacity: getComputedStyle(row.querySelector('.track-row-actions')).opacity,
            actionsColor: getComputedStyle(row.querySelector('.track-row-actions')).color,
            expected,
          };
        }""")
        self.assertEqual("1", rest["menuOpacity"], "desktop row menus must remain rendered at rest")
        self.assertEqual("1", rest["actionsOpacity"], "desktop like actions must remain rendered at rest")
        self.assertEqual(rest["expected"], rest["menuColor"])
        self.assertEqual(rest["expected"], rest["actionsColor"])
        row.hover()
        page.wait_for_timeout(150)
        hover_color = row.locator(".row-menu").evaluate("(button) => getComputedStyle(button).color")
        ink = page.evaluate("""() => {
          const probe = document.createElement('span'); probe.style.color = 'var(--ink)'; document.body.append(probe);
          const color = getComputedStyle(probe).color; probe.remove(); return color;
        }""")
        self.assertEqual(ink, hover_color, "desktop row actions should gain ink emphasis on hover")
        row.locator(".row-menu").click()
        page.wait_for_selector("#context-menu:not([hidden])")
        labels = page.evaluate("() => [...document.querySelectorAll('#context-menu button')].map((button) => button.textContent)")
        self.assertNotIn("Like", labels, f"desktop menu duplicated the row like control: {labels}")
        self.assertNotIn("Unlike", labels, f"desktop menu duplicated the row like control: {labels}")
        self.assertFalse(any(label.startswith("Duration ") for label in labels),
                         f"desktop menu duplicated the row duration: {labels}")

    def test_rows_are_numbered_dated_and_64px(self):
        page = self.page(1440, 900)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_selector(".track-row:not(.track-placeholder)")
        shape = page.evaluate("""() => {
          const row = document.querySelector('.track-row:not(.track-placeholder)');
          return {
            height: Math.round(row.getBoundingClientRect().height),
            ordinal: row.querySelector('.track-ordinal')?.textContent ?? null,
            posted: row.querySelector('.track-posted')?.textContent ?? null,
            intrinsic: getComputedStyle(row).containIntrinsicSize,
            art: Math.round(document.querySelector('.row-art').getBoundingClientRect().width),
          };
        }""")
        self.assertEqual(64, shape["height"])
        self.assertEqual("64px", shape["intrinsic"].split()[-1],
                         "contain-intrinsic-size drifted from the row height, so off-screen rows reserve the wrong space")
        self.assertEqual(48, shape["art"])
        self.assertEqual("01", shape["ordinal"], "the ordinal is the real play position, zero-padded to the total")
        self.assertTrue(shape["posted"], "rows must show when the track was posted")

    def test_library_header_overlays_scroll_content_without_blocking_it(self):
        page = self.page(1440, 900)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_selector('.track-row:not(.track-placeholder)')
        shape = page.evaluate("""() => {
          const library = document.getElementById('library');
          const header = document.querySelector('.library-header');
          const blur = document.querySelector('.library-header-blur');
          const content = document.getElementById('library-content');
          const row = document.querySelector('.track-row:not(.track-placeholder)');
          const input = document.getElementById('track-search');
          const before = {
            libraryTop: library.getBoundingClientRect().top,
            headerTop: header.getBoundingClientRect().top,
            headerBottom: header.getBoundingClientRect().bottom,
            contentTop: content.getBoundingClientRect().top,
            scrollTop: content.scrollTop,
          };
          content.scrollTop = Math.min(content.scrollHeight - content.clientHeight, 180);
          content.dispatchEvent(new Event('scroll'));
          const afterRow = row.getBoundingClientRect();
          const afterHeader = header.getBoundingClientRect();
          const afterBlur = blur?.getBoundingClientRect();
          return {
            blurCount: document.querySelectorAll('.library-header-blur').length,
            libraryTop: before.libraryTop,
            headerTop: afterHeader.top,
            headerBottom: afterHeader.bottom,
            contentTop: before.contentTop,
            scrollDelta: content.scrollTop - before.scrollTop,
            rowTop: afterRow.top,
            rowBottom: afterRow.bottom,
            blurBottom: afterBlur?.bottom ?? 0,
            blurPointerEvents: blur ? getComputedStyle(blur).pointerEvents : '',
            inputId: input.id,
          };
        }""")
        self.assertEqual(1, shape["blurCount"], "the header needs one shared blur plane")
        self.assertAlmostEqual(shape["headerTop"], shape["libraryTop"], delta=1,
                               msg=f"header should stay pinned to the library top: {shape}")
        self.assertGreater(shape["scrollDelta"], 8, f"library-content did not scroll: {shape}")
        self.assertGreater(shape["blurBottom"], shape["headerBottom"],
                           f"blur must fade beyond the visible header: {shape}")
        self.assertLess(shape["rowTop"], shape["headerBottom"],
                        f"a track never moved behind the header: {shape}")
        self.assertGreater(shape["rowBottom"], shape["headerTop"],
                           f"the scrolled track disappeared completely: {shape}")
        self.assertEqual("none", shape["blurPointerEvents"])
        page.locator("#track-search").click()
        self.assertTrue(page.locator("#track-search").evaluate("(input) => document.activeElement === input"))

    def test_source_column_only_appears_across_sources(self):
        page = self.page(1440, 900)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_selector(".track-row:not(.track-placeholder)")
        # All music: the source is the one thing distinguishing otherwise similar rows.
        self.assertTrue(page.evaluate("() => !!document.querySelector('.track-source')"))
        across = page.evaluate("() => getComputedStyle(document.querySelector('.track-source')).display")
        self.assertNotEqual("none", across)
        # Inside one source it repeats the h1 on every row and is the widest column (audit D5).
        page.evaluate("() => document.querySelector('.library').classList.add('single-source')")
        page.wait_for_timeout(80)
        within = page.evaluate("""() => {
          const row = document.querySelector('.track-row:not(.track-placeholder)');
          const visible = (element) => [...element.children]
            .filter((child) => getComputedStyle(child).display !== 'none')
            .map((child) => child.classList.contains('track-row-actions') ? 'track-row-actions' : child.classList.contains('row-menu') ? 'row-menu' : child.classList[0]);
          return {
            source: getComputedStyle(row.querySelector('.track-source')).display,
            headSource: getComputedStyle(document.querySelector('.track-head').children[2]).display,
            rowCells: visible(row),
            rowColumns: getComputedStyle(row).gridTemplateColumns,
          };
        }""")
        self.assertEqual("none", within["source"], "the source column repeats the page title inside a single source")
        self.assertEqual("none", within["headSource"], "the desktop header must hide Source with its cells")
        self.assertEqual(["track-ordinal", "track-main", "track-posted", "track-duration", "track-row-actions", "row-menu"], within["rowCells"])
        self.assertEqual(6, len(within["rowColumns"].split()), "hidden Source must not leave a dead grid track")

    def test_locate_current_sends_the_active_sort_to_position(self):
        page = self.page(1440, 900)
        current = {
            **_tracks(1)[0],
            "metadata": {"title": "Angels", "artist": "Burial"},
            "file": {"name": "angels.mp3", "mimeType": "audio/mpeg"},
        }
        positions = []

        def locate_route(route):
            path = urlsplit(route.request.url).path
            decoded_path = unquote(path)
            if decoded_path == "/api/playback/queue":
                return route.fulfill(status=200, content_type="application/json", body='{"keys": ["-1001:1000"]}')
            if decoded_path.startswith("/api/tracks/") and decoded_path.count("/") == 3:
                return route.fulfill(status=200, content_type="application/json", body=json.dumps(current))
            if path.endswith("/position"):
                positions.append(route.request.url)
                return route.fulfill(status=200, content_type="application/json", body='{"index": 0}')
            return route.fallback()

        page.route("**/api/**", locate_route)
        page.locator(".track-row:not(.track-placeholder) .track-main").nth(0).click()
        page.wait_for_function("() => document.getElementById('player-title').textContent === 'Angels'")
        if page.evaluate("() => document.getElementById('error-dialog').open"):
            page.locator("#error-dialog [data-close='error-dialog']").click()
        page.evaluate("""() => {
          const select = document.getElementById('track-sort');
          select.value = 'title';
          select.dispatchEvent(new Event('change', { bubbles: true }));
          document.getElementById('player-locate').click();
        }""")
        page.wait_for_function("() => document.querySelector('#player-locate').getAttribute('aria-busy') === null")
        self.assertTrue(positions, "locate did not request the current track position")
        self.assertIn("sort=title", positions[-1])

    def test_now_playing_tabs_have_bidirectional_aria_relationships(self):
        page = self.page(1440, 900)
        self.open_now_panel(page)
        relationships = page.evaluate("""() => [...document.querySelectorAll('.now-tabs [role=tab]')].map((tab) => ({
          id: tab.id,
          controls: tab.getAttribute('aria-controls'),
          panelRole: document.getElementById(tab.getAttribute('aria-controls'))?.getAttribute('role'),
          labelledBy: document.getElementById(tab.getAttribute('aria-controls'))?.getAttribute('aria-labelledby'),
        }))""")
        self.assertEqual(3, len(relationships))
        for relationship in relationships:
            self.assertTrue(relationship["controls"])
            self.assertEqual("tabpanel", relationship["panelRole"])
            self.assertEqual(relationship["id"], relationship["labelledBy"])

    def test_lyrics_panel_shows_lrclib_attribution(self):
        page = self.page(1440, 900)
        self.open_now_panel(page)
        attribution = page.locator("#lyrics-attribution")
        self.assertTrue(attribution.is_visible())
        self.assertEqual("Lyrics from LRCLIB", attribution.text_content())

    def test_non_classifying_eyebrows_are_removed(self):
        page = self.page(1440, 900)
        page.route("**/api/tracks*", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"items": [], "offset": 0, "total": 0}'))
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.fill("#track-search", "qqqqq")
        page.wait_for_selector("#empty-library:not([hidden])")
        self.assertEqual(0, page.locator("#empty-eyebrow").count(), "No matches is not a classifying eyebrow")
        self.assertEqual(0, page.locator(".now-header .eyebrow").count(), "Now playing repeats the panel label")
        self.assertIn("qqqqq", page.text_content("#empty-title"))

    def test_responsive_rows_keep_four_cells_at_800px(self):
        page = self.page(800, 900)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_selector(".track-row:not(.track-placeholder)")
        shape = page.evaluate("""() => {
          const visible = (element) => [...element.children]
            .filter((child) => getComputedStyle(child).display !== 'none');
          const head = document.querySelector('.track-head');
          const row = document.querySelector('.track-row:not(.track-placeholder)');
          return {
            rowCells: visible(row).length,
            rowRows: getComputedStyle(row).gridTemplateRows.trim().split(/\\s+/).filter(Boolean).length,
            headDisplay: getComputedStyle(head).display,
            rowColumns: getComputedStyle(row).gridTemplateColumns.trim().split(/\\s+/).filter(Boolean).length,
            visibleClasses: visible(row).map((child) => child.classList.contains('row-menu') ? 'row-menu' : child.classList[0]),
          };
        }""")
        self.assertEqual("none", shape["headDisplay"], "the column head should hide on a narrow layout")
        self.assertEqual(4, shape["rowCells"], "800px rows should expose ordinal, main, posted and menu")
        self.assertEqual(["track-ordinal", "track-main", "track-posted", "row-menu"], shape["visibleClasses"])
        self.assertEqual(4, shape["rowColumns"], "the grid must have four visible tracks at 800px")
        self.assertEqual(1, shape["rowRows"], "a row cell wrapped into an implicit second grid row")

    def test_sort_control_changes_the_requested_order(self):
        page = self.page(1440, 900)
        requested = []

        def record(route):
            requested.append(route.request.url)
            self._stub(route)

        page.route("**/api/tracks*", record)
        # page() has already booted once before returning; reload after installing the recorder
        # so the default request is observed by this regression too.
        page.reload(wait_until="load")
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_selector(".track-row:not(.track-placeholder)")
        self.assertTrue(any("sort=posted" in url for url in requested),
                        f"the default sort is not sent: {requested}")

        requested.clear()
        page.select_option("#track-sort", "title")
        page.wait_for_function("() => document.querySelectorAll('.track-row').length > 0")
        page.wait_for_function("() => document.querySelector('.track-head [data-sort=title]')?.getAttribute('aria-sort') === 'ascending'")
        # It must reach the network, not a cache entry keyed without sort.
        self.assertTrue(any("sort=title" in url for url in requested),
                        f"changing sort served a stale cache entry instead of refetching: {requested}")

        marked = page.evaluate("""() => [...document.querySelectorAll('.track-head [aria-sort]')]
          .map((cell) => [cell.textContent.trim(), cell.getAttribute('aria-sort')])""")
        self.assertIn(["Track", "ascending"], marked,
                      f"the active sort key is not marked in the column header: {marked}")

        requested.clear()
        page.click('.head-sort[data-sort="posted"]')
        page.wait_for_function("() => document.querySelector('.track-head [data-sort=posted]')?.getAttribute('aria-sort') === 'descending'")
        self.assertTrue(any("sort=posted" in url for url in requested),
                        f"clicking the Posted head did not use the sort request path: {requested}")

        requested.clear()
        page.select_option("#track-sort", "title")
        page.wait_for_function("() => document.querySelector('.track-head [data-sort=title]')?.getAttribute('aria-sort') === 'ascending'")
        self.assertTrue(any("sort=title" in url for url in requested),
                        f"returning to Title did not refetch the cached sort key: {requested}")

    def test_all_music_count_uses_server_total_and_starts_neutral(self):
        static_page = self.browser.new_page(viewport={"width": 1440, "height": 900})
        static_page.route("**/app.js", lambda route: route.abort())
        static_page.goto(f"http://127.0.0.1:{self.port}/index.html", wait_until="load")
        self.addCleanup(static_page.close)
        self.assertEqual("—", static_page.text_content("#all-count"), "pre-response state must not claim zero")

        page = self.page(1440, 900)
        page.route("**/api/tracks*", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"items": _tracks(1), "offset": 0, "total": 1,
                             "allMusicTotal": 7, "dayBreaks": []})))
        page.reload(wait_until="load")
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_function("() => document.getElementById('all-count').textContent === '7'")
        self.assertEqual("1 track", page.text_content("#library-summary"),
                         "active-view total must remain query-specific")
        self.assertNotEqual(7, sum(source["trackCount"] for source in SOURCES),
                            "fixture must intentionally disagree with source.trackCount")

    def test_all_music_day_rules_virtualize_without_duplicate_rows_or_spacer_gaps(self):
        page = self.page(1440, 900)
        tracks = _tracks(121)
        day_breaks = [
            {"index": 0, "dayKey": "2025-07-30"},
            {"index": 40, "dayKey": "2025-07-29"},
            {"index": 80, "dayKey": "2025-07-28"},
        ]

        def paged(route):
            query = parse_qs(urlsplit(route.request.url).query)
            offset = int(query.get("offset", [0])[0])
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "items": tracks[offset:offset + 100], "offset": offset, "total": len(tracks),
                "allMusicTotal": len(tracks), "dayBreaks": day_breaks,
            }))

        page.route("**/api/tracks*", paged)
        page.reload(wait_until="load")
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_selector('.day-separator[data-day-key="2025-07-30"]')
        self.assertEqual("── 30 JUL ──", page.text_content('.day-separator[data-day-key="2025-07-30"]'))

        page.evaluate("""() => {
          const library = document.getElementById('library-content');
          library.scrollTop = document.getElementById('track-list').offsetTop + 4300;
          library.dispatchEvent(new Event('scroll'));
        }""")
        page.wait_for_selector('.day-separator[data-day-key="2025-07-28"]')
        geometry = page.evaluate("""() => {
          const list = document.getElementById('track-list');
          const rows = [...list.querySelectorAll('.track-row')];
          const keys = rows.map((row) => row.dataset.trackKey).filter(Boolean);
          const spacer = [...list.querySelectorAll('.track-spacer')]
            .reduce((sum, item) => sum + item.getBoundingClientRect().height, 0);
          const separators = [...list.querySelectorAll('.day-separator')];
          return {
            rows: rows.length,
            unique: new Set(keys).size,
            keyed: keys.length,
            accounted: spacer + rows.length * 52 + separators.length * 28,
          };
        }""")
        self.assertLessEqual(geometry["rows"], 80, "separator rows must not consume the 80-track budget")
        self.assertEqual(geometry["keyed"], geometry["unique"], "a boundary duplicated a track row")
        self.assertEqual(121 * 52 + 3 * 28, geometry["accounted"], "separator height drifted out of spacers")

        page.select_option("#track-sort", "title")
        page.wait_for_function("() => document.querySelector('.track-head [data-sort=title]')?.getAttribute('aria-sort') === 'ascending'")
        self.assertEqual(0, page.locator(".day-separator").count(), "non-posted sorts must suppress rules")

    def test_expanded_now_header_is_capped_and_keeps_its_contents_visible(self):
        for width, height in ((1440, 900), (390, 844)):
            with self.subTest(viewport=(width, height)):
                page = self.page(width, height)
                self.open_now_panel(page)
                geometry = page.evaluate("""() => {
                  const panel = document.getElementById('now-panel').getBoundingClientRect();
                  const header = document.querySelector('.now-header').getBoundingClientRect();
                  const selectors = ['.large-art-wrap', '.now-title', '.now-tabs', '#close-now'];
                  const boxes = selectors.map((selector) => {
                    const element = document.querySelector(selector);
                    const box = element.getBoundingClientRect();
                    return { selector, visible: box.width > 0 && box.height > 0,
                      contained: box.top >= header.top - 1 && box.bottom <= header.bottom + 1 };
                  });
                  return { ratio: header.height / panel.height, overflow: header.scrollHeight > header.clientHeight + 1, boxes };
                }""")
                self.assertLessEqual(geometry["ratio"], .45 + .002)
                self.assertFalse(geometry["overflow"], f"expanded header overflowed at {width}x{height}")
                self.assertTrue(all(item["visible"] and item["contained"] for item in geometry["boxes"]), geometry["boxes"])

    def test_search_results_reuse_the_library_row_system(self):
        page = self.page(1440, 900)
        requested = []

        def search(route):
            payload = json.loads(route.request.post_data or "{}")
            requested.append(payload)
            if payload.get("query") == "cap":
                tracks = _tracks(30)
                sources = []
            else:
                sources = [{
                    "chatId": "-1005", "kind": "bot", "title": "@deepcuts_bot", "username": "deepcuts_bot",
                    "selected": True, "trackCount": 31,
                }]
                tracks = [{
                    "key": "-1009:77", "title": "Burial", "artist": "Burial", "durationMs": 242000,
                    "artworkVersion": "search-v1", "source": {
                        "chatId": "-1009", "title": "Telegram Vault", "kind": "channel", "selected": False,
                    },
                }]
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"sources": sources, "tracks": tracks}))

        # This route must be registered before filling the input: the generic fallback in _stub
        # returns {}, which would exercise the empty state rather than either result template.
        page.route("**/api/search/telegram", search)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.fill("#global-search", "burial")
        page.wait_for_selector("[data-global-track]")
        page.wait_for_function("""() => {
          const image = document.querySelector('[data-global-track] .row-art');
          return image?.complete && image.naturalWidth > 0;
        }""")
        shape = page.evaluate("""() => {
          const source = document.querySelector('[data-global-source]');
          const track = document.querySelector('[data-global-track]');
          const art = track?.querySelector('.row-art');
          const sourceAvatar = source?.querySelector('.source-avatar');
          return {
            trackArt: art ? Math.round(art.getBoundingClientRect().width) : 0,
            trackSrc: art?.getAttribute('src') ?? null,
            trackDataSrc: art?.getAttribute('data-src') ?? null,
            trackLoading: art?.getAttribute('loading') ?? null,
            trackNaturalWidth: art?.naturalWidth ?? 0,
            titlePx: track ? getComputedStyle(track.querySelector('strong')).fontSize : null,
            trackProvenance: track?.querySelector('.result-provenance')?.textContent.trim() ?? null,
            sourceProvenance: source?.querySelector('.result-provenance')?.textContent.trim() ?? null,
            sourceKind: source?.querySelector('.track-copy small')?.textContent.trim() ?? null,
            sourceAvatarRadius: sourceAvatar ? getComputedStyle(sourceAvatar).borderRadius : null,
            sourceDurationCell: source?.querySelector('.track-duration')?.textContent.trim() ?? null,
            trackDuration: track?.querySelector('.track-duration')?.textContent.trim() ?? null,
            marks: document.querySelectorAll('.global-result-mark').length,
            count: document.getElementById('global-results-count')?.textContent.trim() ?? null,
          };
        }""")
        self.assertEqual(40, shape["trackArt"], "the track result must use the library's 40px artwork")
        self.assertTrue(shape["trackSrc"], "track result artwork must use src so it can load in the dropdown")
        self.assertIsNone(shape["trackDataSrc"], "dropdown artwork must not depend on the library-only observer")
        self.assertEqual("lazy", shape["trackLoading"])
        self.assertGreater(shape["trackNaturalWidth"], 0, "the track cover did not load")
        self.assertEqual("15px", shape["titlePx"], "result titles should match library rows (--text-body)")
        self.assertEqual("In your library", shape["sourceProvenance"])
        self.assertEqual("On Telegram", shape["trackProvenance"])
        self.assertEqual("Bot · 31 known tracks", shape["sourceKind"], "source kind should use sourceKindLabel")
        self.assertEqual("50%", shape["sourceAvatarRadius"], "source results retain circular avatars")
        self.assertEqual("", shape["sourceDurationCell"], "source results keep an empty duration cell")
        self.assertEqual("4:02", shape["trackDuration"])
        self.assertEqual(0, shape["marks"], "the four-meaning 8px mark column still exists")
        self.assertEqual("2 results", shape["count"])

        page.fill("#global-search", "cap")
        page.wait_for_function("() => document.getElementById('global-results-count')?.textContent.trim() === 'First 30 results'")
        self.assertTrue(requested, "the search route was not called")
        self.assertTrue(all(request.get("limit") == 30 for request in requested),
                        f"search requests drifted from the server cap: {requested}")

    def test_zero_results_drop_the_column_head_and_disable_play(self):
        page = self.page(1440, 900)
        page.route("**/api/tracks*", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"items": [], "offset": 0, "total": 0}'))
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.fill("#track-search", "qqqqq")
        page.wait_for_selector("#empty-library:not([hidden])")
        state = page.evaluate("""() => ({
          head: getComputedStyle(document.querySelector('.track-head')).display,
          play: document.getElementById('play-playlist').disabled,
          shuffle: document.getElementById('shuffle-playlist').disabled,
        })""")
        self.assertEqual("none", state["head"], "the TRACK/SOURCE/TIME head sits above an empty list")
        self.assertIs(True, state["play"], "Play is enabled with nothing to play")
        self.assertIs(True, state["shuffle"], "Shuffle is enabled with nothing to shuffle")

    def test_label_disc_holds_while_paused_and_rotates_only_while_playing(self):
        page = self.page(1440, 900)
        self.open_now_panel(page)
        paused = page.evaluate("""() => {
          const disc = document.querySelector('.large-art-wrap');
          const style = getComputedStyle(disc);
          const ring = getComputedStyle(disc, '::before');
          const probe = document.createElement('span');
          probe.style.borderTop = '1px solid var(--rule)';
          document.body.append(probe);
          const ruleColor = getComputedStyle(probe).borderTopColor;
          probe.remove();
          return {
            classes: disc.className,
            radius: style.borderRadius,
            square: Math.abs(disc.getBoundingClientRect().width - disc.getBoundingClientRect().height) < 1,
            name: style.animationName,
            duration: style.animationDuration,
            playState: style.animationPlayState,
            ringColor: ring.borderTopColor,
            ruleColor,
          };
        }""")
        self.assertIn("label-disc", paused["classes"])
        self.assertTrue(paused["square"], "a label must be a circle, so the box has to be square")
        self.assertEqual("50%", paused["radius"])
        self.assertEqual("label-spin", paused["name"])
        self.assertEqual("20s", paused["duration"])
        self.assertEqual("paused", paused["playState"], "a paused disc must hold its current angle")
        self.assertEqual(paused["ruleColor"], paused["ringColor"], "the paused ring must use --rule")

        page.evaluate("() => document.querySelector('.label-disc').classList.add('is-playing')")
        playing = page.evaluate("""() => {
          const disc = document.querySelector('.label-disc');
          const style = getComputedStyle(disc);
          const ring = getComputedStyle(disc, '::before');
          const probe = document.createElement('span');
          probe.style.borderTop = '1px solid var(--stamp)';
          document.body.append(probe);
          const stampColor = getComputedStyle(probe).borderTopColor;
          probe.remove();
          return {
            name: style.animationName,
            duration: style.animationDuration,
            playState: style.animationPlayState,
            ringColor: ring.borderTopColor,
            stampColor,
          };
        }""")
        self.assertEqual("label-spin", playing["name"])
        self.assertEqual("20s", playing["duration"])
        self.assertEqual("running", playing["playState"])
        self.assertEqual(playing["stampColor"], playing["ringColor"], "the playing ring must use --stamp")

    def test_label_disc_is_static_for_reduced_motion(self):
        page = self.browser.new_page(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
        page.route("**/api/**", self._stub)
        page.goto(f"http://127.0.0.1:{self.port}/index.html", wait_until="load")
        self.addCleanup(page.close)
        self.open_now_panel(page)
        self.assertIn("label-disc", page.locator(".large-art-wrap").get_attribute("class"))
        page.evaluate("() => document.querySelector('.large-art-wrap').classList.add('is-playing')")
        name = page.evaluate("() => getComputedStyle(document.querySelector('.large-art-wrap')).animationName")
        self.assertEqual("none", name, "reduced-motion users must never get a spinning disc")

    def test_header_flip_does_not_cancel_playing_label_spin(self):
        page = self.page(1440, 900)
        self.open_now_panel(page)
        page.evaluate("""() => {
          const disc = document.querySelector('.label-disc');
          disc.classList.add('is-playing');
          const spacer = document.createElement('div');
          spacer.style.height = '1000px';
          document.getElementById('now-content').append(spacer);
          const content = document.getElementById('now-content');
          content.scrollTop = 100;
          content.dispatchEvent(new Event('scroll'));
        }""")
        page.wait_for_function("() => document.querySelector('.now-header').classList.contains('is-compact')")
        animations = page.evaluate("""() => {
          const disc = document.querySelector('.label-disc');
          const all = disc.getAnimations();
          const spin = all.filter((animation) => animation.animationName === 'label-spin');
          return {
            compact: document.querySelector('.now-header').classList.contains('is-compact'),
            flipCount: all.filter((animation) => animation.animationName !== 'label-spin').length,
            spinCount: spin.length,
            spinState: spin[0]?.playState ?? null,
          };
        }""")
        self.assertTrue(animations["compact"], "the real scroll path must compact the now-playing header")
        self.assertGreaterEqual(animations["flipCount"], 1, "compaction should start the header FLIP animation")
        self.assertEqual(1, animations["spinCount"], "FLIP must not cancel the CSS label-spin animation")
        self.assertEqual("running", animations["spinState"])

    def test_headlines_take_no_terminal_period(self):
        page = self.page(1440, 900)
        page.route("**/api/tracks*", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"items": [], "offset": 0, "total": 0}'))
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.fill("#track-search", "qqqqq")
        page.wait_for_selector("#empty-library:not([hidden])")
        headline = page.text_content("#empty-title")
        self.assertFalse(headline.rstrip().endswith("."), f"headline carries a terminal period: {headline!r}")
        # The best-written state in the app (audit F): it must still name the query.
        self.assertIn("qqqqq", headline)

    def test_field_token_inset_band(self):
        """Fields must read as a recess, not a hole, on both themes (impeccable audit).

        Dark: the field sits strictly between the canvas and the raised dialog
        surface. Light: the field is a visible recess (darker than the canvas)
        but never so dark that it falls out of the surface family. The --field
        token is what keeps every inset honest, so pin the band here.
        """
        page = self.page(1440, 900)
        for theme in ("dark", "light"):
            page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
            colors = page.evaluate("""() => {
                const root = getComputedStyle(document.documentElement);
                const read = (token) => {
                    const probe = document.createElement('i');
                    probe.style.background = token.trim();
                    document.body.append(probe);
                    const value = getComputedStyle(probe).backgroundColor;
                    probe.remove();
                    return value.match(/\\d+(\\.\\d+)?/g).slice(0, 3).map(Number);
                };
                return {
                    paper: read(root.getPropertyValue('--paper')),
                    field: read(root.getPropertyValue('--field')),
                    raised: read(root.getPropertyValue('--surface-raised')),
                };
            }""")
            paper = _rel_lum(colors["paper"])
            field = _rel_lum(colors["field"])
            raised = _rel_lum(colors["raised"])
            if theme == "dark":
                self.assertGreaterEqual(field, paper + 0.004, (theme, paper, field, raised))
                self.assertLessEqual(field, raised - 0.002, (theme, paper, field, raised))
            else:
                self.assertLessEqual(field, paper - 0.03, (theme, paper, field))
                self.assertGreaterEqual(field, raised - 0.25, (theme, field, raised))
