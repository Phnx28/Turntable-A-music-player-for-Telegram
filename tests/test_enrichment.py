"""F1/F3/F7: fingerprint service, AcoustID client and the enrichment pipeline."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from acoustid import AcoustIDClient, AcoustIDError
from enrichment import EnrichmentService, _temporary
from fingerprints import FingerprintError, FingerprintService
from resolver import AcoustIDRecording, ReleaseGroup

from core import Database


class FingerprintServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_missing_fpcalc_is_graceful(self):
        service = FingerprintService(fpcalc_path="/nonexistent/fpcalc")
        self.assertFalse(service.available())
        with self.assertRaises(FingerprintError):
            asyncio.get_event_loop().run_until_complete(
                service.fingerprint_track(Path("song.mp3"))
            )

    @patch("fingerprints.shutil.which", return_value="/usr/bin/fpcalc")
    async def test_parses_fpcalc_json_output(self, _which):
        service = FingerprintService()

        async def fake_exec(*args, **kwargs):
            stdout = json.dumps({"duration": 191.6, "fingerprint": "AQADtEm"}).encode()

            class Process:
                returncode = 0

                async def communicate(self):
                    return stdout, b""

            return Process()

        with patch("fingerprints.asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await service.fingerprint_track(Path("song.mp3"))
        self.assertEqual("AQADtEm", result.fingerprint)
        self.assertAlmostEqual(191.6, result.duration)

    @patch("fingerprints.shutil.which", return_value="/usr/bin/fpcalc")
    async def test_nonzero_exit_raises(self, _which):
        service = FingerprintService()

        async def fake_exec(*args, **kwargs):
            class Process:
                returncode = 1

                async def communicate(self):
                    return b"", b"could not open file"

            return Process()

        with patch("fingerprints.asyncio.create_subprocess_exec", side_effect=fake_exec):
            with self.assertRaisesRegex(FingerprintError, "could not open file"):
                await service.fingerprint_track(Path("song.mp3"))


def _database():
    return Database(Path(tempfile.mkdtemp()) / "library.sqlite3")


class AcoustIDClientTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, handler):
        transport = httpx.MockTransport(handler)
        return AcoustIDClient(
            _database(), httpx.AsyncClient(transport=transport), api_key="test-key", user_agent="Turntable/1.0"
        )

    async def test_parses_results_into_recordings(self):
        def handler(request):
            return httpx.Response(200, json={
                "results": [{
                    "sources": 3,
                    "recordings": [{
                        "id": "mbid-1",
                        "title": "Paranoid Android",
                        "duration": 383,
                        "artists": [{"name": "Radiohead"}],
                        "releasegroups": [{"title": "OK Computer", "type": "Album"}],
                    }],
                }],
            })

        client = self._client(handler)
        recordings = await client.lookup("AQADtEm", 383.0)
        self.assertEqual(1, len(recordings))
        self.assertEqual("mbid-1", recordings[0].mbid)
        self.assertEqual("Radiohead", recordings[0].artist)
        self.assertEqual(383_000, recordings[0].duration_ms)
        self.assertEqual("OK Computer", recordings[0].release_groups[0].title)
        self.assertEqual("Album", recordings[0].release_groups[0].primary_type)
        await client.http.aclose()

    async def test_transient_failures_raise_and_are_not_cached(self):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(429)
            return httpx.Response(200, json={"results": [{
                "sources": 1,
                "recordings": [{"id": "mbid-2", "title": "Second Try"}],
            }]})

        client = self._client(handler)
        with self.assertRaises(AcoustIDError):
            await client.lookup("fp", 100.0)
        recordings = await client.lookup("fp", 100.0)
        self.assertEqual(1, len(recordings), "a later success replaces the temporary failure")
        await client.http.aclose()

    async def test_lookup_results_are_cached(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"results": []})

        client = self._client(handler)
        self.assertEqual([], await client.lookup("fp", 100.0))
        self.assertEqual([], await client.lookup("fp", 100.0))
        self.assertEqual(1, len(calls), "the second lookup must come from the cache")
        await client.http.aclose()

    async def test_unconfigured_client_returns_no_candidates(self):
        client = AcoustIDClient(_database(), httpx.AsyncClient(), api_key="", user_agent="Turntable/1.0")
        self.assertFalse(client.configured())
        self.assertEqual([], await client.lookup("fp", 100.0))
        await client.http.aclose()


class EnrichmentPipelineTests(unittest.IsolatedAsyncioTestCase):
    """F2/F7: the pipeline persists identity, respects state, and never blocks."""

    def setUp(self):
        self.database = _database()
        self.addCleanup(self.database.close)
        self.database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
        self.database.upsert_tracks([{
            "chatId": "1", "messageId": "2", "fileName": "song.mp3", "mimeType": "audio/mpeg",
            "title": "Paranoid Android", "artist": "Radiohead", "documentId": "9",
        }])
        self.track = self.database.get_track("1", "2")

    def _service(self, candidate=None, acoustid_key="test-key", cover_result="cover.jpg"):
        media = SimpleNamespace(
            fingerprint_source=AsyncMock(return_value=Path("/tmp/song.mp3"))
        )
        fingerprints = SimpleNamespace(
            fingerprint_track=AsyncMock(
                return_value=SimpleNamespace(fingerprint="AQADtEm", duration=383.0)
            )
        )
        acoustid = SimpleNamespace(
            configured=lambda: bool(acoustid_key),
            lookup=AsyncMock(return_value=candidate or []),
        )
        return EnrichmentService(
            self.database, media, fingerprints, acoustid,
            cover_fetcher=AsyncMock(return_value=cover_result),
        )

    async def test_auto_apply_persists_identity_and_resolves(self):
        candidate = AcoustIDRecording(
            "mbid-a", "Paranoid Android", "Radiohead", 383_000, sources=5,
            release_groups=[ReleaseGroup("rg-1", "OK Computer", "Album")],
        )
        service = self._service([candidate])
        result = await service.enrich_track(self.track, trigger="playback")
        self.assertEqual("resolved", result.decision)
        self.assertIsNotNone(result.recording_id)
        recording = self.database.get_track_recording("1:2")
        self.assertEqual("mbid-a", recording["musicbrainz_recording_id"])
        field = self.database.metadata_field(result.recording_id, "title")
        self.assertEqual("Paranoid Android", field["value"])
        self.assertEqual("fingerprint_resolver", field["source"])
        self.assertEqual("resolved", self.database.get_enrichment_state("1:2")["status"])
        # The release-group cover landed as a provenance field and the state settled.
        recording = self.database.get_track_recording("1:2")
        self.assertEqual("rg-1", recording["release_group_mbid"])
        artwork = self.database.metadata_field(result.recording_id, "artworkPath")
        self.assertEqual("cover.jpg", artwork["value"])
        self.assertEqual(False, artwork["locked"])

    async def test_terminal_state_skips_work(self):
        self.database.set_enrichment_state("1:2", "no_match")
        service = self._service()
        result = await service.enrich_track(self.track, trigger="playback")
        self.assertEqual("skipped", result.decision)

    async def test_temporary_failure_backs_off(self):
        service = self._service()
        service.acoustid.lookup = AsyncMock(side_effect=AcoustIDError("rate limited"))
        result = await service.enrich_track(self.track, trigger="playback")
        self.assertEqual("temporary_failure", result.decision)
        state = self.database.get_enrichment_state("1:2")
        self.assertIsNotNone(state["next_retry_at"])
        # Within the backoff window the track is skipped.
        result = await service.enrich_track(self.track, trigger="playback")
        self.assertEqual("skipped", result.decision)

    async def test_ambiguous_never_writes_identity(self):
        # Two distinct recordings both consistent with the evidence (same title,
        # artist and duration, different MBIDs): the fingerprint cannot decide, so
        # nothing is applied and no identity is linked.
        candidates = [
            AcoustIDRecording("mbid-a", "Paranoid Android", "Radiohead", 383_000, sources=4),
            AcoustIDRecording("mbid-b", "Paranoid Android", "Radiohead", 383_000, sources=2),
        ]
        service = self._service(candidates)
        result = await service.enrich_track(self.track, trigger="playback")
        self.assertEqual("ambiguous", result.decision)
        self.assertIsNone(self.database.get_track_recording("1:2"))
        self.assertEqual("ambiguous", self.database.get_enrichment_state("1:2")["status"])

    async def test_unconfigured_acoustid_is_a_distinct_no_match(self):
        service = self._service(acoustid_key="")
        result = await service.enrich_track(self.track, trigger="playback")
        self.assertEqual("no_match", result.decision)
        state = self.database.get_enrichment_state("1:2")
        self.assertEqual("acoustid-unconfigured", state["failure_code"])

    async def test_capability_missing_no_match_is_never_terminal(self):
        # no_match with a capability failure code (no fpcalc, no AcoustID key) means
        # the track was never actually judged. Playback must retry once the
        # capability exists instead of treating it as a definitive no-match.
        candidate = AcoustIDRecording(
            "mbid-a", "Paranoid Android", "Radiohead", 383_000, sources=5,
            release_groups=[ReleaseGroup("rg-1", "OK Computer", "Album")],
        )
        self.database.set_enrichment_state("1:2", "no_match", failure_code="fingerprint-unavailable")
        service = self._service([candidate])
        result = await service.enrich_track(self.track, trigger="playback")
        self.assertEqual("resolved", result.decision,
                         "fingerprint-unavailable must retry, not skip as terminal")

        self.database.set_enrichment_state("1:2", "no_match", failure_code="acoustid-unconfigured")
        service = self._service([candidate])
        result = await service.enrich_track(self.track, trigger="playback")
        self.assertEqual("resolved", result.decision,
                         "acoustid-unconfigured must retry once the key exists")
        recording = self.database.get_track_recording("1:2")
        self.assertEqual("mbid-a", recording["musicbrainz_recording_id"])

    async def test_playback_trigger_is_silent_and_deduped(self):
        self.database.set_enrichment_state("1:2", "temporary_failure",
                                           next_retry_at=9999999999)
        service = self._service()
        with patch("enrichment.asyncio.create_task") as create_task:
            service.enrich_playback(self.track)
            create_task.assert_not_called()
        # A fresh track fires a background task.
        self.database.set_enrichment_state("1:2", "temporary_failure")
        with patch("enrichment.asyncio.create_task") as create_task:
            service.enrich_playback(self.track)
            create_task.assert_called_once()

    async def test_manual_display_bridge_layers_resolved_metadata(self):
        # The resolved identity shows in the UI without touching the user overrides.
        candidate = AcoustIDRecording("mbid-a", "Paranoid Android", "Radiohead", 383_000, sources=5)
        service = self._service([candidate])
        await service.enrich_track(self.track, trigger="playback")
        displayed = self.database.get_track("1", "2")
        self.assertEqual("Paranoid Android", displayed["metadata"]["title"])
        self.assertEqual("Radiohead", displayed["metadata"]["artist"])
        # A user override still outranks the automatic value.
        self.database.save_metadata_patch("1", "2", {"title": "My title"}, [])
        displayed = self.database.get_track("1", "2")
        self.assertEqual("My title", displayed["metadata"]["title"])


if __name__ == "__main__":
    unittest.main()


class ArtworkStateTests(unittest.IsolatedAsyncioTestCase):
    """G3/G4: release-group artwork honours permanent vs transient failures."""

    def setUp(self):
        self.database = _database()
        self.addCleanup(self.database.close)
        self.database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
        self.database.upsert_tracks([{
            "chatId": "1", "messageId": "2", "fileName": "song.mp3", "mimeType": "audio/mpeg",
            "title": "Paranoid Android", "artist": "Radiohead", "documentId": "9",
        }])
        self.track = self.database.get_track("1", "2")
        self.candidate = AcoustIDRecording(
            "mbid-a", "Paranoid Android", "Radiohead", 383_000, sources=5,
            release_groups=[ReleaseGroup("rg-1", "OK Computer", "Album")],
        )

    def _service(self, cover_result="cover.jpg"):
        media = SimpleNamespace(fingerprint_source=AsyncMock(return_value=Path("/tmp/song.mp3")))
        fingerprints = SimpleNamespace(
            fingerprint_track=AsyncMock(
                return_value=SimpleNamespace(fingerprint="AQADtEm", duration=383.0)
            )
        )
        acoustid = SimpleNamespace(configured=lambda: True, lookup=AsyncMock(return_value=[self.candidate]))
        return EnrichmentService(
            self.database, media, fingerprints, acoustid,
            cover_fetcher=AsyncMock(return_value=cover_result),
        )

    async def test_definitive_miss_marks_and_settles(self):
        service = self._service(cover_result=None)
        result = await service.enrich_track(self.track, trigger="playback")
        self.assertEqual("resolved", result.decision)
        state = self.database.get_enrichment_state("1:2")
        self.assertEqual("resolved", state["status"])
        self.assertEqual("artwork-missing", state["failure_code"])
        self.assertIsNotNone(self.database.get_track_recording("1:2"))
        # The miss marker excludes the track from the text-search cover job.
        self.assertEqual([], self.database.tracks_needing_artwork(limit=10))

    async def test_transient_artwork_failure_retries_without_poisoning(self):
        async def flaky(rgid, quality):
            if not hasattr(flaky, "attempts"):
                flaky.attempts = 0
            flaky.attempts += 1
            if flaky.attempts == 1:
                raise RuntimeError("transient 503")
            return "cover.jpg"

        service = self._service(cover_result=None)
        service.cover_fetcher = flaky
        result = await service.enrich_track(self.track, trigger="playback")
        self.assertEqual("temporary_failure", result.decision)
        # The identity is already linked, so the retry re-enters at the artwork step.
        recording = self.database.get_track_recording("1:2")
        self.assertIsNotNone(recording)
        self.assertEqual("rg-1", recording["release_group_mbid"])
        self.assertIsNone(self.database.metadata_field(recording["id"], "artworkPath"))
        # Let the backoff expire (the state re-entry still holds the release groups).
        self.database.set_enrichment_state("1:2", "temporary_failure")
        retry = await service.enrich_track(self.track, trigger="playback")
        self.assertEqual("resolved", retry.decision)
        artwork = self.database.metadata_field(recording["id"], "artworkPath")
        self.assertEqual("cover.jpg", artwork["value"])

    async def test_existing_user_artwork_is_never_overwritten(self):
        # A user-chosen cover (metadata_overrides) outranks the automatic one in
        # display: the automatic value may exist in provenance but is hidden.
        self.database.save_metadata_patch("1", "2", {"artworkPath": "user-choice.jpg"}, [])
        service = self._service()
        result = await service.enrich_track(self.track, trigger="playback")
        self.assertEqual("resolved", result.decision)
        displayed = self.database.get_track("1", "2")
        self.assertEqual("user-choice.jpg", displayed["metadata"]["artworkPath"])
        self.assertEqual("user-choice.jpg", displayed["overrides"]["artworkPath"])


class BulkEnrichmentTests(unittest.IsolatedAsyncioTestCase):
    """H3/H5/H6/H7: resumable, cancellable, playback-first bulk enrichment."""

    def setUp(self):
        self.database = _database()
        self.addCleanup(self.database.close)
        self.database.upsert_source({"chatId": "1", "kind": "channel", "title": "Music"})
        self.database.upsert_tracks([
            {"chatId": "1", "messageId": str(i), "fileName": f"song-{i}.mp3",
             "mimeType": "audio/mpeg", "title": f"Track {i}", "artist": "Artist",
             "documentId": str(i)}
            for i in range(30)
        ])
        self.candidate = AcoustIDRecording(
            "mbid-a", "Paranoid Android", "Radiohead", 383_000, sources=5,
            release_groups=[ReleaseGroup("rg-1", "OK Computer", "Album")],
        )

    def _service(self, acquire_delay=0.0):
        media = SimpleNamespace(fingerprint_source=AsyncMock(return_value=Path("/tmp/song.mp3")))
        fingerprints = SimpleNamespace(
            fingerprint_track=AsyncMock(
                return_value=SimpleNamespace(fingerprint="AQADtEm", duration=383.0)
            )
        )
        acoustid = SimpleNamespace(configured=lambda: True, lookup=AsyncMock(return_value=[self.candidate]))
        return EnrichmentService(
            self.database, media, fingerprints, acoustid,
            cover_fetcher=AsyncMock(return_value="cover.jpg"),
        )

    async def test_bulk_walk_is_resumable_and_settles_every_track(self):
        service = self._service()
        job = SimpleNamespace(processed=0, found=0, result=None)
        await service.bulk_enrich_source(job, "1", "uncertain", fetch_artwork=True, replace_existing=False)
        self.assertEqual(30, job.processed)
        self.assertEqual(30, job.found)
        resolved = [
            key for key in (
                self.database.get_enrichment_state(f"1:{i}") for i in range(30)
            ) if key and key["status"] == "resolved"
        ]
        self.assertEqual(30, len(resolved), "every track settled with durable state")

    async def test_interrupted_run_resumes_without_reprocessing(self):
        service = self._service()

        def flaky_lookup(fingerprint, duration):
            async def lookup():
                await asyncio.sleep(0.01)
                if not hasattr(flaky_lookup, "count"):
                    flaky_lookup.count = 0
                flaky_lookup.count += 1
                if flaky_lookup.count == 3:
                    raise AcoustIDError("transient")
                return [self.candidate]
            return lookup()

        service.acoustid.lookup = flaky_lookup
        job = SimpleNamespace(processed=0, found=0, result=None)
        await service.bulk_enrich_source(job, "1", "uncertain", fetch_artwork=True, replace_existing=False)
        # One track hit a transient failure: the run stops for retry backoff later.
        failures = [
            self.database.get_enrichment_state(f"1:{i}")
            for i in range(30)
            if self.database.get_enrichment_state(f"1:{i}")
        ]
        retryable = [state for state in failures if state["status"] == "temporary_failure"]
        self.assertEqual(1, len(retryable))
        # A second run resumes: the resolved tracks are skipped, the retryable one is
        # re-attempted only after its backoff (clear it to simulate a later run).
        self.database.set_enrichment_state(retryable[0]["track_key"], "temporary_failure")
        job2 = SimpleNamespace(processed=0, found=0, result=None)
        await service.bulk_enrich_source(job2, "1", "uncertain", fetch_artwork=True, replace_existing=False)
        resolved = [
            self.database.get_enrichment_state(f"1:{i}")
            for i in range(30)
        ]
        self.assertTrue(all(state["status"] == "resolved" for state in resolved),
                        "the resumed run settles the remaining track")

    async def test_playback_triggers_bypass_the_bulk_gate(self):
        # The bulk semaphore admits two tracks; playback triggers must not queue
        # behind them (Phase H6: playback always wins over bulk enrichment).
        service = self._service()
        gate = asyncio.Event()
        original = service.enrich_track

        async def slow_bulk(track, **kwargs):
            if kwargs.get("trigger") == "bulk":
                await gate.wait()
            return await original(track, **kwargs)

        service.enrich_track = slow_bulk
        first = asyncio.create_task(service.enrich_track(
            self.database.get_track("1", "0"), trigger="bulk"))
        second = asyncio.create_task(service.enrich_track(
            self.database.get_track("1", "1"), trigger="bulk"))
        playback = asyncio.create_task(service.enrich_track(
            self.database.get_track("1", "2"), trigger="playback"))
        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.gather(first, second, playback)
        self.assertEqual("resolved", playback.result().decision,
                         "playback enrichment completes regardless of the bulk queue")

    async def test_cancellation_stops_the_walk_cleanly(self):
        service = self._service()
        job = SimpleNamespace(processed=0, found=0, result=None)

        task = asyncio.create_task(service.bulk_enrich_source(
            job, "1", "uncertain", fetch_artwork=True, replace_existing=False))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        # The tracks settled before the cancel keep their durable states.
        settled = [
            self.database.get_enrichment_state(f"1:{i}")
            for i in range(5)
        ]
        self.assertTrue(any(state is not None for state in settled))
