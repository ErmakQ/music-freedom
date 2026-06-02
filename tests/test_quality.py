from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import pack_downloads
import quality


def candidate(title: str, provider: str = "hitmo:http") -> quality.TrackCandidate:
    artist, raw_title = ("", title)
    if " - " in title:
        artist, raw_title = title.split(" - ", 1)
    return quality.TrackCandidate(
        provider=provider,
        input_track="Artist - Song",
        raw_artist=artist or None,
        raw_title=raw_title,
        raw_display_title=title,
        source_url="https://example.test/source",
        download_url="https://example.test/file.mp3",
    )


class QualityTests(unittest.TestCase):
    def test_normalize_track_text(self) -> None:
        self.assertEqual(
            quality.normalize_track_text("Artist feat. Someone — Track"),
            "artist feat someone track",
        )

    def test_detect_requested_version_flags(self) -> None:
        self.assertEqual(quality.detect_requested_version_flags("Artist - Song"), set())
        self.assertIn("radio", quality.detect_requested_version_flags("Artist - Song Radio Edit"))
        self.assertIn("remix", quality.detect_requested_version_flags("Artist - Song Remix"))
        self.assertIn("explicit", quality.detect_requested_version_flags("Artist - Song Explicit"))

    def test_clean_version_rejected(self) -> None:
        flags = quality.detect_candidate_version_flags("Artist - Song Clean Version")
        rejected, reason = quality.should_hard_reject_candidate(set(), flags)
        self.assertTrue(rejected)
        self.assertEqual(reason, quality.STATUS_REJECTED_CLEAN_VERSION)

    def test_censored_version_rejected(self) -> None:
        flags = quality.detect_candidate_version_flags("Artist - Song Censored")
        rejected, reason = quality.should_hard_reject_candidate(set(), flags)
        self.assertTrue(rejected)
        self.assertEqual(reason, quality.STATUS_REJECTED_CENSORED_VERSION)

    def test_radio_edit_rejected_unless_requested(self) -> None:
        flags = quality.detect_candidate_version_flags("Artist - Song Radio Edit")
        rejected, reason = quality.should_hard_reject_candidate(set(), flags)
        self.assertTrue(rejected)
        self.assertEqual(reason, quality.STATUS_REJECTED_RADIO_VERSION)

        requested = quality.detect_requested_version_flags("Artist - Song Radio Edit")
        rejected, reason = quality.should_hard_reject_candidate(requested, flags)
        self.assertFalse(rejected)
        self.assertIsNone(reason)

    def test_remix_and_live_rejected_unless_requested(self) -> None:
        remix_flags = quality.detect_candidate_version_flags("Artist - Song Remix")
        rejected, reason = quality.should_hard_reject_candidate(set(), remix_flags)
        self.assertTrue(rejected)
        self.assertEqual(reason, quality.STATUS_REJECTED_REMIX_VERSION)

        live_flags = quality.detect_candidate_version_flags("Artist - Song Live")
        rejected, reason = quality.should_hard_reject_candidate(set(), live_flags)
        self.assertTrue(rejected)
        self.assertEqual(reason, quality.STATUS_REJECTED_LIVE_VERSION)

        requested = quality.detect_requested_version_flags("Artist - Song Remix")
        rejected, _reason = quality.should_hard_reject_candidate(requested, remix_flags)
        self.assertFalse(rejected)

    def test_uncensored_russian_marker_is_positive(self) -> None:
        flags = quality.detect_candidate_version_flags("Artist - Song Без цензуры")
        self.assertIn("uncensored", flags)
        self.assertNotIn("censored", flags)
        rejected, reason = quality.should_hard_reject_candidate(set(), flags)
        self.assertFalse(rejected)
        self.assertIsNone(reason)

    def test_explicit_marker_boosts_score(self) -> None:
        normal = candidate("Artist - Song")
        explicit = candidate("Artist - Song Explicit")
        normal_score = quality.score_candidate("Artist - Song", normal).final_score
        explicit_score = quality.score_candidate("Artist - Song", explicit).final_score
        self.assertGreaterEqual(explicit_score, normal_score)
        self.assertFalse(explicit.hard_reject)

    def test_post_download_threshold_is_less_strict_than_search_ok_threshold(self) -> None:
        self.assertLess(quality.POST_DOWNLOAD_OK_SCORE_THRESHOLD, quality.OK_SCORE_THRESHOLD)
        self.assertGreaterEqual(quality.POST_DOWNLOAD_OK_SCORE_THRESHOLD, quality.LOW_CONFIDENCE_THRESHOLD)

    def test_select_best_candidate_skips_hard_reject(self) -> None:
        clean = candidate("Artist - Song Clean Version")
        original = candidate("Artist - Song")
        selected, reason, ranked = quality.select_best_candidate("Artist - Song", [clean, original])
        self.assertIs(selected, original)
        self.assertIsNone(reason)
        self.assertTrue(any(c.hard_reject for c in ranked))

    def test_duration_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "track.mp3"
            path.write_bytes(b"0" * (20 * 1024))
            cand = candidate("Artist - Song")
            cand.duration_sec = 200

            old_reader = quality._read_audio_metadata
            quality._read_audio_metadata = lambda _path: ({}, 100, 192000)  # type: ignore[assignment]
            try:
                result = quality.validate_downloaded_file(str(path), "Artist - Song", cand)
            finally:
                quality._read_audio_metadata = old_reader  # type: ignore[assignment]

        self.assertFalse(result.ok)
        self.assertEqual(result.status, quality.STATUS_VERSION_MISMATCH)

    def test_manual_review_csv_generation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            item = quality.make_quality_item(
                "Artist - Song",
                quality.STATUS_REJECTED_RADIO_VERSION,
                reason="radio",
                selected_candidate=candidate("Artist - Song Radio Edit"),
            )
            files = quality.write_quality_reports(td, [item], [], total=1)
            manual = Path(files["manual_review_csv"]).read_text(encoding="utf-8")
            report = json.loads(Path(files["quality_report_json"]).read_text(encoding="utf-8"))

        self.assertIn("Artist - Song", manual)
        self.assertEqual(report["summary"]["manual_review"], 1)

    def test_pack_downloads_uses_accepted_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accepted = root / "accepted"
            rejected = root / "rejected"
            accepted.mkdir()
            rejected.mkdir()
            (accepted / "ok.mp3").write_bytes(b"ok")
            (rejected / "bad.mp3").write_bytes(b"bad")
            out = root / "result.zip"

            code = pack_downloads.build_zip(root, out)
            self.assertEqual(code, 0)
            with zipfile.ZipFile(out) as zf:
                self.assertEqual(zf.namelist(), ["ok.mp3"])


if __name__ == "__main__":
    unittest.main()
