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

    def test_rows_are_numbered_dated_and_52px(self):
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
        self.assertEqual(52, shape["height"])
        self.assertEqual("52px", shape["intrinsic"].split()[-1],
                         "contain-intrinsic-size drifted from the row height, so off-screen rows reserve the wrong space")
        self.assertEqual(40, shape["art"])
        self.assertEqual("01", shape["ordinal"], "the ordinal is the real play position, zero-padded to the total")
        self.assertTrue(shape["posted"], "rows must show when the track was posted")

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
        within = page.evaluate("() => getComputedStyle(document.querySelector('.track-source')).display")
        self.assertEqual("none", within, "the source column repeats the page title inside a single source")

    def test_responsive_rows_keep_head_compatible_at_800px(self):
        page = self.page(800, 900)
        page.evaluate("() => { document.getElementById('app-shell').hidden = false; }")
        page.wait_for_selector(".track-row:not(.track-placeholder)")
        shape = page.evaluate("""() => {
          const visible = (element) => [...element.children]
            .filter((child) => getComputedStyle(child).display !== 'none');
          const head = document.querySelector('.track-head');
          const row = document.querySelector('.track-row:not(.track-placeholder)');
          return {
            headCells: visible(head).length,
            rowCells: visible(row).length,
            headRows: getComputedStyle(head).gridTemplateRows.trim().split(/\\s+/).filter(Boolean).length,
            rowRows: getComputedStyle(row).gridTemplateRows.trim().split(/\\s+/).filter(Boolean).length,
          };
        }""")
        self.assertEqual(6, shape["headCells"], "Source is hidden at 800px, leaving six visible head cells")
        self.assertEqual(shape["headCells"], shape["rowCells"], "head and row must expose the same visible cells")
        self.assertEqual(1, shape["headRows"], "the column head must stay a single grid row")
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
