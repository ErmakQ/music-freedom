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

    def test_generate_search_queries_keeps_extra_dash_in_title(self) -> None:
        queries = quality.generate_search_queries("tripperbeats, Flo Fire, LeMoris - Ride - DnB")

        self.assertIn("tripperbeats, Flo Fire, LeMoris Ride - DnB", queries)
        self.assertIn("Ride - DnB tripperbeats, Flo Fire, LeMoris", queries)
        self.assertNotIn("tripperbeats, Flo Fire, LeMoris - Ride DnB", queries)

    def test_generate_search_queries_strips_prod_noise(self) -> None:
        queries = quality.generate_search_queries("SQWOZ BAB - 300 (prod.by DJ AUX)")

        self.assertIn("SQWOZ BAB - 300", queries)
        self.assertIn("SQWOZ BAB 300", queries)
        self.assertIn("300 SQWOZ BAB", queries)

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

    def test_requested_remix_is_not_penalized_in_score(self) -> None:
        normal = candidate("Artist - Song")
        remix = candidate("Artist - Song Remix")

        remix_score = quality.score_candidate("Artist - Song Remix", remix).final_score
        normal_score = quality.score_candidate("Artist - Song", normal).final_score

        self.assertGreaterEqual(remix_score, normal_score - 0.02)
        self.assertFalse(remix.hard_reject)

    def test_low_bass_reupload_is_rejected(self) -> None:
        flags = quality.detect_candidate_version_flags("31-35Hz Artist - Song Low Bass By Danka")
        rejected, reason = quality.should_hard_reject_candidate(set(), flags)
        self.assertIn("bassboost", flags)
        self.assertTrue(rejected)
        self.assertEqual(reason, quality.STATUS_REJECTED_REMIX_VERSION)

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

    def test_transliterated_match_scores_above_threshold(self) -> None:
        candidate_track = quality.TrackCandidate(
            provider="hitmo:http",
            input_track="Ямыч Восточный Округ - Не строю планы",
            raw_artist="YAmych Vostochnyjj Okrug",
            raw_title="Ne stroju plany",
            raw_display_title="YAmych Vostochnyjj Okrug - Ne stroju plany",
            source_url="https://example.test/source",
            download_url="https://example.test/file.mp3",
        )

        score = quality.score_candidate("Ямыч Восточный Округ - Не строю планы", candidate_track)

        self.assertGreaterEqual(score.final_score, quality.LOW_CONFIDENCE_THRESHOLD)
        self.assertGreaterEqual(score.artist_score, 0.75)
        self.assertGreaterEqual(score.title_score, 0.75)

    def test_transliterated_ptica_scores_above_post_download_threshold(self) -> None:
        candidate_track = quality.TrackCandidate(
            provider="hitmo:http",
            input_track="TB Step, AlvinToday - \u041f\u0442\u0438\u0446\u0430",
            raw_artist="TB Step AlvinToday",
            raw_title="Ptica",
            raw_display_title="TB Step AlvinToday - Ptica",
            source_url="https://example.test/source",
            download_url="https://example.test/file.mp3",
        )

        score = quality.score_candidate("TB Step, AlvinToday - \u041f\u0442\u0438\u0446\u0430", candidate_track)

        self.assertGreaterEqual(score.artist_score, 0.95)
        self.assertGreaterEqual(score.title_score, 0.8)
        self.assertGreaterEqual(score.final_score, quality.POST_DOWNLOAD_OK_SCORE_THRESHOLD)

    def test_transliterated_shjolk_scores_above_post_download_threshold(self) -> None:
        candidate_track = quality.TrackCandidate(
            provider="hitmo:http",
            input_track="\u0412\u0430\u043d\u044f \u0414\u043c\u0438\u0442\u0440\u0438\u0435\u043d\u043a\u043e - \u0428\u0451\u043b\u043a",
            raw_artist="Vanya Dmitrienko",
            raw_title="SHjolk",
            raw_display_title="Vanya Dmitrienko - SHjolk",
            source_url="https://example.test/source",
            download_url="https://example.test/file.mp3",
        )

        score = quality.score_candidate("\u0412\u0430\u043d\u044f \u0414\u043c\u0438\u0442\u0440\u0438\u0435\u043d\u043a\u043e - \u0428\u0451\u043b\u043a", candidate_track)

        self.assertGreaterEqual(score.artist_score, 0.95)
        self.assertGreaterEqual(score.title_score, 0.8)
        self.assertGreaterEqual(score.final_score, quality.POST_DOWNLOAD_OK_SCORE_THRESHOLD)

    def test_feat_and_prod_noise_do_not_break_match(self) -> None:
        candidate_track = quality.TrackCandidate(
            provider="skysound:http",
            input_track="ICEGERGERT - Псих",
            raw_artist="ICEGERGERT",
            raw_title="Psih prod.by Nura",
            raw_display_title="ICEGERGERT - Psih prod.by Nura",
            source_url="https://example.test/source",
            download_url="https://example.test/file.mp3",
        )

        score = quality.score_candidate("ICEGERGERT - Псих", candidate_track)

        self.assertGreaterEqual(score.artist_score, 0.95)
        self.assertGreaterEqual(score.title_score, 0.8)

    def test_repeated_letter_title_can_match_translit_variant(self) -> None:
        candidate_track = quality.TrackCandidate(
            provider="hitmo:http",
            input_track="GAMORA - Аййй",
            raw_artist="GAMORA",
            raw_title="Ajjjjjj",
            raw_display_title="GAMORA - Ajjjjjj",
            source_url="https://example.test/source",
            download_url="https://example.test/file.mp3",
        )

        score = quality.score_candidate("GAMORA - Аййй", candidate_track)

        self.assertGreaterEqual(score.artist_score, 0.95)
        self.assertGreaterEqual(score.title_score, 0.75)

    def test_single_string_candidate_display_can_still_score_well(self) -> None:
        candidate_track = quality.TrackCandidate(
            provider="lmusic:http",
            input_track="Порнофильмы - В диапазоне",
            raw_artist=None,
            raw_title="Порнофильмы В диапазоне",
            raw_display_title="Порнофильмы В диапазоне",
            source_url="https://example.test/source",
            download_url="https://example.test/file.mp3",
        )

        score = quality.score_candidate("Порнофильмы - В диапазоне", candidate_track)

        self.assertGreaterEqual(score.final_score, quality.POST_DOWNLOAD_OK_SCORE_THRESHOLD)

    def test_artist_only_match_is_capped_when_title_is_wrong(self) -> None:
        wrong = quality.TrackCandidate(
            provider="zvyki:http",
            input_track="SQWOZ BAB - Репост",
            raw_artist="SQWOZ BAB",
            raw_title="TOKYO",
            raw_display_title="SQWOZ BAB - TOKYO",
            source_url="https://example.test/source",
            download_url="https://example.test/tokyo.mp3",
        )
        correct = quality.TrackCandidate(
            provider="hitmo:http",
            input_track="SQWOZ BAB - Репост",
            raw_artist="SQWOZ BAB",
            raw_title="Repost",
            raw_display_title="SQWOZ BAB - Repost",
            source_url="https://example.test/source",
            download_url="https://example.test/repost.mp3",
        )

        wrong_score = quality.score_candidate("SQWOZ BAB - Репост", wrong)
        correct_score = quality.score_candidate("SQWOZ BAB - Репост", correct)

        self.assertLess(wrong_score.final_score, quality.LOW_CONFIDENCE_THRESHOLD)
        self.assertGreater(correct_score.final_score, wrong_score.final_score)
        self.assertLess(wrong_score.title_score, 0.35)

    def test_short_single_token_title_requires_artist_match(self) -> None:
        wrong_artist = quality.TrackCandidate(
            provider="skysound:http",
            input_track="SAVVY - Кома",
            raw_artist="SALUKI",
            raw_title="Koma",
            raw_display_title="SALUKI - Koma",
            source_url="https://example.test/source",
            download_url="https://example.test/koma.mp3",
        )

        score = quality.score_candidate("SAVVY - Кома", wrong_artist)

        self.assertGreaterEqual(score.title_score, 0.9)
        self.assertLess(score.artist_score, 0.55)
        self.assertLess(score.final_score, quality.LOW_CONFIDENCE_THRESHOLD)

    def test_short_single_token_title_with_correct_artist_is_not_capped(self) -> None:
        correct = quality.TrackCandidate(
            provider="skysound:http",
            input_track="SQWOZ BAB - 300 (prod.by DJ AUX)",
            raw_artist="SQWOZ BAB",
            raw_title="300",
            raw_display_title="SQWOZ BAB - 300",
            source_url="https://example.test/source",
            download_url="https://example.test/300.mp3",
        )

        score = quality.score_candidate("SQWOZ BAB - 300 (prod.by DJ AUX)", correct)

        self.assertGreaterEqual(score.artist_score, 0.95)
        self.assertGreaterEqual(score.title_score, 0.95)
        self.assertGreaterEqual(score.final_score, quality.POST_DOWNLOAD_OK_SCORE_THRESHOLD)

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
