from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import quality
from download_service import load_download_config_from_env
from downloader_core import DownloadConfig, _candidate_report_entry, run_playlist_download
import downloader_core
from search.base import ProviderBase
from search.cache import SearchCache
from search.models import ProviderResult, SearchCandidate, TrackQuery
from search.orchestrator import SearchOrchestrator


def track_query(raw: str = "Artist - Song") -> TrackQuery:
    return TrackQuery(
        raw=raw,
        artist="Artist",
        title="Song",
        generated_queries=[raw],
        requested_flags=[],
    )


def search_candidate(
    provider: str = "hitmo:http",
    title: str = "Artist - Song",
    url: str = "https://example.test/get/music/song.mp3",
) -> SearchCandidate:
    return SearchCandidate(
        provider=provider,
        input_track="Artist - Song",
        search_query="Artist - Song",
        source_url="https://example.test/search",
        download_url=url,
        referer="https://example.test/search",
        raw_artist="Artist",
        raw_title="Song",
        raw_display_title=title,
        decision_reason=f"{provider}_fixture",
    )


class FakeProvider(ProviderBase):
    def __init__(self, name: str, candidates: list[SearchCandidate] | None = None, delay: float = 0.0) -> None:
        self.name = name
        self.candidates = candidates or []
        self.delay = delay
        self.calls = 0

    async def search(self, track: TrackQuery) -> ProviderResult:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return ProviderResult(self.name, self.candidates, reason=None if self.candidates else "miss")


class SearchLayerContractTests(unittest.TestCase):
    def test_search_orchestrator_http_first(self) -> None:
        http = FakeProvider("hitmo:http", [search_candidate()])
        selenium = FakeProvider("selenium:fallback", [search_candidate("selenium:fallback")])

        decision = asyncio.run(
            SearchOrchestrator(
                [http],
                selenium_provider=selenium,
                enable_selenium_fallback=True,
            ).search(track_query())
        )

        self.assertEqual(http.calls, 1)
        self.assertEqual(selenium.calls, 0)
        self.assertEqual(decision.decision_reason, "early_stop_http_high_confidence")

    def test_selenium_disabled_by_default(self) -> None:
        cfg = DownloadConfig()
        self.assertFalse(cfg.enable_selenium_fallback)

        http = FakeProvider("hitmo:http", [])
        selenium = FakeProvider("selenium:fallback", [search_candidate("selenium:fallback")])
        asyncio.run(
            SearchOrchestrator(
                [http],
                selenium_provider=selenium,
                enable_selenium_fallback=cfg.enable_selenium_fallback,
            ).search(track_query())
        )

        self.assertEqual(selenium.calls, 0)

    def test_no_chrome_required_in_balanced_mode(self) -> None:
        cfg = DownloadConfig(search_mode="balanced", enable_selenium_fallback=False)
        fake_resolve = {
            "download_candidates": [],
            "all_candidates": [],
            "selected_candidate": None,
            "requested_flags": [],
            "generated_queries": ["Artist - Song"],
            "status": quality.STATUS_NOT_FOUND,
            "reason": quality.STATUS_NOT_FOUND,
            "decision_reason": "test_http_only",
            "provider_results": [],
            "from_cache": False,
        }
        with tempfile.TemporaryDirectory() as td:
            with patch("downloader_core.create_driver", side_effect=AssertionError("Chrome should not start")):
                with patch("downloader_core._resolve_quality_plan", return_value=fake_resolve):
                    result = run_playlist_download(["Artist - Song"], td, cfg=cfg)

        self.assertEqual(result["ok"], [])
        self.assertEqual(result["failed"], ["Artist - Song"])

    def test_resolve_quality_plan_runs_async_orchestrator(self) -> None:
        class FakeDecision:
            candidates = []
            selected_candidate = None
            status = quality.STATUS_NOT_FOUND
            reason = quality.STATUS_NOT_FOUND
            decision_reason = "test_async_orchestrator"
            provider_results = []
            from_cache = False

        async def fake_search(self, track):
            return FakeDecision()

        cfg = DownloadConfig(search_cache_enabled=False, enable_selenium_fallback=False)
        with patch("search.orchestrator.SearchOrchestrator.search", fake_search):
            result = downloader_core._resolve_quality_plan(None, None, cfg, "Artist - Song")

        self.assertEqual(result["decision_reason"], "test_async_orchestrator")
        self.assertEqual(result["status"], quality.STATUS_NOT_FOUND)

    def test_selenium_used_only_in_deep_mode(self) -> None:
        old = os.environ.copy()
        try:
            os.environ["SEARCH_MODE"] = "balanced"
            os.environ["ENABLE_SELENIUM_FALLBACK"] = "false"
            self.assertFalse(load_download_config_from_env().enable_selenium_fallback)

            os.environ["SEARCH_MODE"] = "deep"
            os.environ["ENABLE_SELENIUM_FALLBACK"] = "false"
            self.assertTrue(load_download_config_from_env().enable_selenium_fallback)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_cache_hit_skips_providers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = SearchCache(Path(td) / "search.sqlite3", enabled=True)
            first = FakeProvider("hitmo:http", [search_candidate()])
            asyncio.run(SearchOrchestrator([first], cache=cache).search(track_query()))

            second = FakeProvider("hitmo:http", [search_candidate(url="https://example.test/other.mp3")])
            decision = asyncio.run(SearchOrchestrator([second], cache=cache).search(track_query()))

            self.assertEqual(second.calls, 0)
            self.assertTrue(decision.from_cache)

    def test_negative_cache_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = SearchCache(Path(td) / "search.sqlite3", enabled=True)
            first = FakeProvider("hitmo:http", [])
            first_decision = asyncio.run(SearchOrchestrator([first], cache=cache).search(track_query()))
            self.assertEqual(first_decision.status, quality.STATUS_NOT_FOUND)

            second = FakeProvider("hitmo:http", [search_candidate()])
            second_decision = asyncio.run(SearchOrchestrator([second], cache=cache).search(track_query()))

            self.assertEqual(second.calls, 0)
            self.assertTrue(second_decision.from_cache)
            self.assertEqual(second_decision.status, quality.STATUS_NOT_FOUND)

    def test_negative_cache_low_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = SearchCache(Path(td) / "search.sqlite3", enabled=True)
            weak = search_candidate(
                title="Completely Different Track",
                url="https://example.test/get/music/noise.mp3",
            )
            weak.raw_artist = "Other"
            weak.raw_title = "Noise"
            first = FakeProvider("hitmo:http", [weak])
            first_decision = asyncio.run(SearchOrchestrator([first], cache=cache).search(track_query()))
            self.assertEqual(first_decision.status, quality.STATUS_LOW_CONFIDENCE_MATCH)

            second = FakeProvider("hitmo:http", [search_candidate()])
            second_decision = asyncio.run(SearchOrchestrator([second], cache=cache).search(track_query()))

            self.assertEqual(second.calls, 0)
            self.assertTrue(second_decision.from_cache)
            self.assertEqual(second_decision.status, quality.STATUS_LOW_CONFIDENCE_MATCH)

    def test_early_stop_on_high_confidence(self) -> None:
        http = FakeProvider("hitmo:http", [search_candidate()])
        selenium = FakeProvider("selenium:fallback", [search_candidate("selenium:fallback")])
        decision = asyncio.run(
            SearchOrchestrator(
                [http],
                selenium_provider=selenium,
                enable_selenium_fallback=True,
            ).search(track_query())
        )

        self.assertEqual(selenium.calls, 0)
        self.assertIsNone(decision.status)
        self.assertGreaterEqual(decision.candidates[0].score, quality.OK_SCORE_THRESHOLD)

    def test_weak_candidate_count_does_not_block_selenium(self) -> None:
        weak_candidates = []
        for i in range(5):
            weak = search_candidate(
                title=f"Wrong Track {i}",
                url=f"https://example.test/get/music/wrong-{i}.mp3",
            )
            weak.raw_artist = "Other"
            weak.raw_title = f"Wrong {i}"
            weak.raw_display_title = f"Other - Wrong {i}"
            weak_candidates.append(weak)
        http = FakeProvider("lmusic:http", weak_candidates)
        selenium = FakeProvider("selenium:fallback", [search_candidate("selenium:fallback")])

        decision = asyncio.run(
            SearchOrchestrator(
                [http],
                selenium_provider=selenium,
                enable_selenium_fallback=True,
            ).search(track_query())
        )

        self.assertEqual(selenium.calls, 1)
        self.assertEqual(decision.decision_reason, "http_plus_selenium_fallback")

    def test_candidates_json_contains_decision_reason(self) -> None:
        decision = asyncio.run(SearchOrchestrator([FakeProvider("hitmo:http", [search_candidate()])]).search(track_query()))
        report = _candidate_report_entry(
            decision.track.raw,
            decision.track.requested_flags,
            decision.track.generated_queries,
            decision.candidates,
            decision.decision_reason,
            decision.provider_results,
            decision.from_cache,
        )

        self.assertIn("decision_reason", report)
        self.assertIn("provider_results", report)
        self.assertIn("from_cache", report)
        self.assertIn("decision_reason", report["candidates"][0])

    def test_playlist_duration_is_preserved_for_validation(self) -> None:
        tq = track_query("Artist - Song")
        tq.expected_duration_sec = 177
        decision = asyncio.run(SearchOrchestrator([FakeProvider("hitmo:http", [search_candidate()])]).search(tq))

        self.assertEqual(decision.candidates[0].duration_sec, 177)

    def test_download_concurrency_not_equal_search_concurrency(self) -> None:
        old = os.environ.copy()
        try:
            os.environ["HTTP_SEARCH_CONCURRENCY"] = "30"
            os.environ["DOWNLOAD_CONCURRENCY"] = "5"
            os.environ["SELENIUM_MAX_WORKERS"] = "1"
            cfg = load_download_config_from_env()
        finally:
            os.environ.clear()
            os.environ.update(old)

        self.assertEqual(cfg.http_search_concurrency, 30)
        self.assertEqual(cfg.download_concurrency, 5)
        self.assertEqual(cfg.selenium_max_workers, 1)
        self.assertNotEqual(cfg.http_search_concurrency, cfg.download_concurrency)


if __name__ == "__main__":
    unittest.main()
