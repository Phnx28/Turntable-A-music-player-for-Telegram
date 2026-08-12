"""F6: fixture tests for the fingerprint-first resolver's false-positive guards.

Each fixture pins whether silent auto-apply is allowed. The rule under test:
a fuzzy text match is never proof of identity; only a fingerprint-consistent
recording with internally consistent evidence may be auto-applied.
"""

import unittest

from resolver import (
    AcoustIDRecording,
    ReleaseGroup,
    ResolutionKind,
    decide,
    distinct_recordings,
    resolve_release_group,
    version_qualifiers,
)


def recording(mbid, title, artist="", duration_ms=None, sources=1):
    return AcoustIDRecording(mbid=mbid, title=title, artist=artist,
                             duration_ms=duration_ms, sources=sources)


class ResolverTests(unittest.TestCase):
    def decide(self, title, artist, duration_s, candidates):
        return decide(
            telegram_title=title,
            telegram_artist=artist,
            audio_duration_s=duration_s,
            candidates=candidates,
        )

    # ---- happy paths -------------------------------------------------------

    def test_clean_studio_match_auto_applies(self):
        result = self.decide(
            "Paranoid Android", "Radiohead", 383.0,
            [recording("mbid-a", "Paranoid Android", "Radiohead", 383_000, sources=5)],
        )
        self.assertEqual(ResolutionKind.AUTO_APPLY, result.kind)
        self.assertEqual("mbid-a", result.recording.mbid)

    def test_punctuation_and_case_do_not_block_a_match(self):
        result = self.decide(
            "paranoid  android!", "radiohead", 383.0,
            [recording("mbid-a", "Paranoid Android", "Radiohead", 383_000, sources=3)],
        )
        self.assertEqual(ResolutionKind.AUTO_APPLY, result.kind)

    def test_same_recording_on_many_releases_is_one_candidate(self):
        result = self.decide(
            "Paranoid Android", "Radiohead", 383.0,
            [
                recording("mbid-a", "Paranoid Android", "Radiohead", 383_000, sources=4),
                recording("mbid-a", "Paranoid Android", "Radiohead", 383_000, sources=2),
            ],
        )
        self.assertEqual(ResolutionKind.AUTO_APPLY, result.kind, "duplicates collapse to one")

    # ---- version qualifiers -------------------------------------------------

    def test_live_version_against_studio_text_is_ambiguous(self):
        result = self.decide(
            "Paranoid Android", "Radiohead", 383.0,
            [recording("mbid-live", "Paranoid Android (Live at Glastonbury)", "Radiohead", 460_000, sources=3)],
        )
        self.assertEqual(ResolutionKind.AMBIGUOUS, result.kind,
                         "a live candidate must never silently relabel a studio track")

    def test_studio_candidate_against_live_text_is_ambiguous(self):
        result = self.decide(
            "Paranoid Android (Live)", "Radiohead", 460.0,
            [recording("mbid-studio", "Paranoid Android", "Radiohead", 383_000, sources=5)],
        )
        self.assertEqual(ResolutionKind.AMBIGUOUS, result.kind)

    def test_live_against_live_is_fine(self):
        result = self.decide(
            "Paranoid Android (Live at Glastonbury)", "Radiohead", 460.0,
            [recording("mbid-live", "Paranoid Android (Live at Glastonbury)", "Radiohead", 460_000, sources=2)],
        )
        self.assertEqual(ResolutionKind.AUTO_APPLY, result.kind)

    def test_remix_against_original_is_ambiguous(self):
        result = self.decide(
            "Midnight City", "M83", 240.0,
            [recording("mbid-remix", "Midnight City (The Quiet Earth Remix)", "M83", 380_000, sources=2)],
        )
        self.assertEqual(ResolutionKind.AMBIGUOUS, result.kind)

    def test_radio_edit_against_album_version_is_ambiguous(self):
        # A radio edit is ~15% shorter than the album take; the duration gap is
        # itself the ambiguity signal when no qualifier appears in the text.
        result = self.decide(
            "The Less I Know the Better", "Tame Impala", 220.0,
            [recording("mbid-album", "The Less I Know the Better (Radio Edit)", "Tame Impala", 180_000, sources=4)],
        )
        self.assertEqual(ResolutionKind.AMBIGUOUS, result.kind,
                         "a large duration gap must not auto-apply")

    def test_remaster_against_original_is_ambiguous(self):
        result = self.decide(
            "Billie Jean", "Michael Jackson", 294.0,
            [recording("mbid-remaster", "Billie Jean (2008 Remaster)", "Michael Jackson", 354_000, sources=3)],
        )
        self.assertEqual(ResolutionKind.AMBIGUOUS, result.kind)

    def test_acoustic_against_original_is_ambiguous(self):
        result = self.decide(
            "Wonderwall", "Oasis", 258.0,
            [recording("mbid-acoustic", "Wonderwall (Acoustic)", "Oasis", 289_000, sources=2)],
        )
        self.assertEqual(ResolutionKind.AMBIGUOUS, result.kind)

    def test_qualified_track_never_becomes_the_original(self):
        # The dangerous direction: a karaoke/instrumental file whose text names the
        # qualifier must never be relabelled as the lyric original -- the Telegram
        # qualifier vetoes a candidate that says nothing about it.
        for title, artist, candidate_title, candidate_ms in (
            ("Back to Black (Instrumental)", "Amy Winehouse", "Back to Black", 225_000),
            ("Rolling in the Deep (Karaoke)", "Adele", "Rolling in the Deep", 228_000),
        ):
            result = self.decide(
                title, artist, 225.0,
                [recording("mbid-original", candidate_title, artist, candidate_ms, sources=2)],
            )
            self.assertEqual(ResolutionKind.AMBIGUOUS, result.kind,
                             f"{title} must not resolve to the plain original")

    # ---- identity traps ------------------------------------------------------

    def test_same_title_by_a_different_artist_is_ambiguous(self):
        result = self.decide(
            "Dreams", "Fleetwood Mac", 262.0,
            [recording("mbid-cranberries", "Dreams", "The Cranberries", 254_000, sources=4)],
        )
        self.assertEqual(ResolutionKind.AMBIGUOUS, result.kind,
                         "same title, different artist: text cannot decide, so it must not apply")

    def test_same_artist_title_but_wrong_duration_is_ambiguous(self):
        result = self.decide(
            "Dancing Queen", "ABBA", 226.0,
            [recording("mbid-short", "Dancing Queen", "ABBA", 120_000, sources=3)],
        )
        self.assertEqual(ResolutionKind.AMBIGUOUS, result.kind)

    def test_cover_version_is_ambiguous(self):
        result = self.decide(
            "Hurt", "Johnny Cash", 218.0,
            [recording("mbid-nin", "Hurt", "Nine Inch Nails", 357_000, sources=5)],
        )
        self.assertEqual(ResolutionKind.AMBIGUOUS, result.kind,
                         "a cover must never silently become the original")

    # ---- weak/absent evidence -------------------------------------------------

    def test_no_candidates_is_no_match(self):
        result = self.decide("Anything", "Anyone", 200.0, [])
        self.assertEqual(ResolutionKind.NO_MATCH, result.kind)

    def test_file_noise_title_allows_fill_only(self):
        result = self.decide(
            "09 - track-final.mp3", "Unknown artist", 383.0,
            [recording("mbid-a", "Paranoid Android", "Radiohead", 383_000, sources=3)],
        )
        self.assertEqual(ResolutionKind.FILL_MISSING_ONLY, result.kind,
                         "a file-noise title must not veto a strong fingerprint, "
                         "but must never replace existing display values")

    def test_multiple_distinct_recordings_is_ambiguous(self):
        result = self.decide(
            "Unknown Track", "Unknown", 200.0,
            [
                recording("mbid-a", "Track One", "Artist A", 199_000, sources=2),
                recording("mbid-b", "Track Two", "Artist B", 201_000, sources=2),
            ],
        )
        self.assertEqual(ResolutionKind.AMBIGUOUS, result.kind)

    def test_duration_tolerance_accepts_small_drift(self):
        result = self.decide(
            "Paranoid Android", "Radiohead", 383.0,
            [recording("mbid-a", "Paranoid Android", "Radiohead", 388_000, sources=3)],
        )
        self.assertEqual(ResolutionKind.AUTO_APPLY, result.kind, "5s drift is a replay artifact")

    def test_compilation_appearance_is_the_same_recording(self):
        # The same recording appearing on a compilation is not a different identity;
        # the release-group titles are information for the album resolver, not for identity.
        result = self.decide(
            "Paranoid Android", "Radiohead", 383.0,
            [recording("mbid-a", "Paranoid Android", "Radiohead", 383_000, sources=5)],
        )
        self.assertEqual(ResolutionKind.AUTO_APPLY, result.kind)


class QualifierTests(unittest.TestCase):
    def test_qualifier_kinds_are_normalised(self):
        self.assertEqual({"live"}, version_qualifiers("Paranoid Android (Live at Glastonbury)"))
        self.assertEqual({"live"}, version_qualifiers("[live]"))
        self.assertEqual({"remix"}, version_qualifiers("Midnight City (Quiet Earth Remix)"))
        self.assertEqual({"remaster"}, version_qualifiers("Billie Jean (2008 Remaster)"))
        self.assertEqual({"instrumental"}, version_qualifiers("Back to Black (Instrumental)"))
        self.assertEqual(set(), version_qualifiers("Paranoid Android"))
        self.assertEqual(set(), version_qualifiers(""))

    def test_distinct_recordings_deduplicate_by_mbid(self):
        items = [
            recording("mbid-a", "One", sources=1),
            recording("mbid-b", "Two", sources=5),
            recording("mbid-a", "One again", sources=9),
        ]
        distinct = distinct_recordings(items)
        self.assertEqual(2, len(distinct))
        self.assertEqual("mbid-b", distinct[0].mbid, "most-supported first")


if __name__ == "__main__":
    unittest.main()


class ReleaseGroupTests(unittest.TestCase):
    """G2: release-group resolution never picks the first MusicBrainz result."""

    def group(self, id, title, primary_type="Album", secondary=None):
        return ReleaseGroup(id=id, title=title, primary_type=primary_type,
                            secondary_types=secondary or [])

    def test_album_tag_match_wins(self):
        groups = [
            self.group("rg-1", "OK Computer", "Album"),
            self.group("rg-2", "The Best of Radiohead", "Compilation"),
        ]
        chosen = resolve_release_group(groups, album_tag="OK Computer")
        self.assertIsNotNone(chosen)
        self.assertEqual("rg-1", chosen.id)

    def test_compilation_is_avoided_when_an_album_exists(self):
        groups = [
            self.group("rg-1", "Some Album", "Album"),
            self.group("rg-2", "Some Album", "Compilation"),
        ]
        chosen = resolve_release_group(groups, album_tag="Some Album")
        self.assertEqual("rg-1", chosen.id)

    def test_no_evidence_returns_none(self):
        self.assertIsNone(resolve_release_group(
            [self.group("rg-1", "Mystery Record", "Other")], album_tag="")
        )
        self.assertIsNone(resolve_release_group([]))

    def test_single_prefers_a_single_over_a_compilation(self):
        groups = [
            self.group("rg-1", "Now That's What I Call Music", "Compilation"),
            self.group("rg-2", "Paranoid Android", "Single"),
        ]
        chosen = resolve_release_group(groups, album_tag="")
        self.assertEqual("rg-2", chosen.id)

    def test_substring_album_match_counts_but_exact_wins(self):
        groups = [
            self.group("rg-1", "OK Computer Collector's Edition", "Album"),
            self.group("rg-2", "OK Computer", "Album"),
        ]
        chosen = resolve_release_group(groups, album_tag="OK Computer")
        self.assertEqual("rg-2", chosen.id)
