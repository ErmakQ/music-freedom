from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import quality
from search.base import ProviderBase
from search.cache import SearchCache
from search.models import ProviderResult, SearchCandidate, TrackQuery
from search.orchestrator import SearchOrchestrator


def query(
    raw: str = "Artist - Song",
    *,
    search_mode: str = "balanced",
    enable_selenium_fallback: bool = False,
) -> TrackQuery:
    return TrackQuery(
        raw=raw,
        artist="Artist",
        title="Song",
        generated_queries=[raw],
        requested_flags=[],
        cache_context={
            "search_mode": search_mode,
            "enabled_providers": ["hitmo:http", "zvyki:http", "lmusic:http"],
            "enable_selenium_fallback": enable_selenium_fallback,
            "provider_origins": {
                "hitmo": "https://eu.hitmoz.com",
                "lmusic": "https://lmusic.kz",
                "zvyki": "https://zvyki.com",
            },
            "search_logic_version": "search-v2",
        },
    )


def candidate(
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
    def __init__(self, name: str, candidates: list[SearchCandidate] | None = None) -> None:
        self.name = name
        self.candidates = candidates or []
        self.calls = 0

    async def search(self, track: TrackQuery) -> ProviderResult:
        self.calls += 1
        return ProviderResult(self.name, self.candidates, reason=None if self.candidates else "miss")


class SearchOrchestratorTests(unittest.TestCase):
    @staticmethod
    def weak_candidate(raw_artist: str, raw_title: str, url_suffix: str) -> SearchCandidate:
        display = f"{raw_artist} - {raw_title}"
        return SearchCandidate(
            provider="hitmo:http",
            input_track="Artist - Song",
            search_query="Artist - Song",
            source_url="https://example.test/search",
            download_url=f"https://example.test/{url_suffix}.mp3",
            referer="https://example.test/search",
            raw_artist=raw_artist,
            raw_title=raw_title,
            raw_display_title=display,
            decision_reason="hitmo:http_fixture_weak",
        )

    def test_http_providers_run_before_selenium(self) -> None:
        http = FakeProvider("hitmo:http", [candidate()])
        selenium = FakeProvider("selenium:fallback", [candidate("selenium:fallback")])
        decision = asyncio.run(
            SearchOrchestrator(
                [http],
                selenium_provider=selenium,
                enable_selenium_fallback=True,
            ).search(query())
        )

        self.assertEqual(http.calls, 1)
        self.assertEqual(selenium.calls, 0)
        self.assertEqual(decision.decision_reason, "early_stop_http_high_confidence")

    def test_selenium_not_called_when_disabled(self) -> None:
        http = FakeProvider("hitmo:http", [])
        selenium = FakeProvider("selenium:fallback", [candidate("selenium:fallback")])
        decision = asyncio.run(
            SearchOrchestrator(
                [http],
                selenium_provider=selenium,
                enable_selenium_fallback=False,
            ).search(query())
        )

        self.assertEqual(http.calls, 1)
        self.assertEqual(selenium.calls, 0)
        self.assertEqual(decision.status, quality.STATUS_NOT_FOUND)

    def test_selenium_called_when_enabled_and_http_misses(self) -> None:
        http = FakeProvider("hitmo:http", [])
        selenium = FakeProvider("selenium:fallback", [candidate("selenium:fallback")])
        decision = asyncio.run(
            SearchOrchestrator(
                [http],
                selenium_provider=selenium,
                enable_selenium_fallback=True,
            ).search(query())
        )

        self.assertEqual(http.calls, 1)
        self.assertEqual(selenium.calls, 1)
        self.assertIsNone(decision.status)
        self.assertEqual(decision.decision_reason, "http_plus_selenium_fallback")

    def test_early_stop_works_with_good_http_result(self) -> None:
        http = FakeProvider("hitmo:http", [candidate()])
        selenium = FakeProvider("selenium:fallback", [candidate("selenium:fallback")])

        decision = asyncio.run(
            SearchOrchestrator(
                [http],
                selenium_provider=selenium,
                enable_selenium_fallback=True,
            ).search(query())
        )

        self.assertEqual(selenium.calls, 0)
        self.assertIsNone(decision.status)
        self.assertGreaterEqual(decision.candidates[0].score, quality.OK_SCORE_THRESHOLD)

    def test_selenium_runs_when_http_only_has_multiple_weak_matches(self) -> None:
        http = FakeProvider(
            "hitmo:http",
            [
                self.weak_candidate("Other", "Song", "other-song"),
                self.weak_candidate("Artist", "Other", "artist-other"),
                self.weak_candidate("Band", "Song", "band-song"),
            ],
        )
        selenium = FakeProvider("selenium:fallback", [candidate("selenium:fallback")])

        decision = asyncio.run(
            SearchOrchestrator(
                [http],
                selenium_provider=selenium,
                enable_selenium_fallback=True,
            ).search(query())
        )

        self.assertEqual(http.calls, 1)
        self.assertEqual(selenium.calls, 1)
        self.assertEqual(decision.decision_reason, "http_plus_selenium_fallback")

    def test_cache_hit_does_not_call_providers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = SearchCache(Path(td) / "search.sqlite3", enabled=True)
            first = FakeProvider("hitmo:http", [candidate()])
            asyncio.run(SearchOrchestrator([first], cache=cache).search(query()))

            second = FakeProvider("hitmo:http", [candidate(url="https://example.test/other.mp3")])
            decision = asyncio.run(SearchOrchestrator([second], cache=cache).search(query()))

            self.assertEqual(second.calls, 0)
            self.assertTrue(decision.from_cache)
            self.assertEqual(decision.cache_hit_type, "positive")

    def test_rejected_candidates_are_not_downloadable(self) -> None:
        clean = candidate(title="Artist - Song Clean Version")
        decision = asyncio.run(SearchOrchestrator([FakeProvider("hitmo:http", [clean])]).search(query()))

        self.assertEqual(decision.status, quality.STATUS_REJECTED_CLEAN_VERSION)
        self.assertTrue(decision.candidates[0].hard_reject)

    def test_not_found_is_cached(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = SearchCache(Path(td) / "search.sqlite3", enabled=True)
            first = FakeProvider("hitmo:http", [])
            first_decision = asyncio.run(SearchOrchestrator([first], cache=cache).search(query()))
            self.assertEqual(first_decision.status, quality.STATUS_NOT_FOUND)

            second = FakeProvider("hitmo:http", [candidate()])
            second_decision = asyncio.run(SearchOrchestrator([second], cache=cache).search(query()))

            self.assertEqual(second.calls, 0)
            self.assertTrue(second_decision.from_cache)
            self.assertEqual(second_decision.status, quality.STATUS_NOT_FOUND)
            self.assertEqual(second_decision.cache_hit_type, "negative")

    def test_cache_key_includes_search_mode(self) -> None:
        fast_key = SearchCache.key_for(query(search_mode="fast"))
        deep_key = SearchCache.key_for(query(search_mode="deep"))

        self.assertNotEqual(fast_key, deep_key)

    def test_cache_key_includes_selenium_flag(self) -> None:
        disabled_key = SearchCache.key_for(query(enable_selenium_fallback=False))
        enabled_key = SearchCache.key_for(query(enable_selenium_fallback=True))

        self.assertNotEqual(disabled_key, enabled_key)

    def test_deep_bypasses_negative_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = SearchCache(
                Path(td) / "search.sqlite3",
                enabled=True,
                bypass_negative_cache_for_deep=True,
            )
            first = FakeProvider("hitmo:http", [])
            first_decision = asyncio.run(
                SearchOrchestrator([first], cache=cache).search(query(search_mode="deep"))
            )
            self.assertEqual(first_decision.status, quality.STATUS_NOT_FOUND)

            second = FakeProvider("hitmo:http", [candidate()])
            second_decision = asyncio.run(
                SearchOrchestrator([second], cache=cache).search(query(search_mode="deep"))
            )

            self.assertEqual(second.calls, 1)
            self.assertFalse(second_decision.from_cache)
            self.assertIsNone(second_decision.status)

    def test_positive_cache_still_works_for_deep(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = SearchCache(
                Path(td) / "search.sqlite3",
                enabled=True,
                bypass_negative_cache_for_deep=True,
            )
            first = FakeProvider("hitmo:http", [candidate()])
            asyncio.run(
                SearchOrchestrator([first], cache=cache).search(query(search_mode="deep"))
            )

            second = FakeProvider("hitmo:http", [candidate(url="https://example.test/other.mp3")])
            second_decision = asyncio.run(
                SearchOrchestrator([second], cache=cache).search(query(search_mode="deep"))
            )

            self.assertEqual(second.calls, 0)
            self.assertTrue(second_decision.from_cache)
            self.assertEqual(second_decision.cache_hit_type, "positive")

    def test_candidate_report_contains_decision_reason(self) -> None:
        decision = asyncio.run(SearchOrchestrator([FakeProvider("hitmo:http", [candidate()])]).search(query()))
        report = decision.to_candidate_report()

        self.assertIn("decision_reason", report)
        self.assertIn("decision_reason", report["candidates"][0])
        self.assertIn("cache_key", report)
        self.assertIn("cache_hit_type", report)
        self.assertIn("skipped_selenium_reason", report)

    def test_cached_skysound_candidates_are_reparsed_without_branding_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = SearchCache(Path(td) / "search.sqlite3", enabled=True)
            payload = {
                "status": None,
                "reason": None,
                "decision_reason": "cache_hit",
                "candidates": [
                    {
                        "provider": "skysound:http",
                        "input_track": "ROUSEMEBABY - SEXY MJ",
                        "raw_artist": "ROUSEMEBABY",
                        "raw_title": "SEXY MJ (SkySound.cc)",
                        "raw_display_title": "ROUSEMEBABY - SEXY MJ (SkySound.cc)",
                        "download_url": "https://fine.sunproxy.net/file/token/ROUSEMEBABY_-_SEXY_MJ_(SkySound.cc).mp3",
                        "referer": "https://rousemebaby.skysound7.com/t/123-rousemebaby-sexy-mj/",
                        "source_url": "https://rousemebaby.skysound7.com/t/123-rousemebaby-sexy-mj/",
                        "search_query": "ROUSEMEBABY - SEXY MJ",
                    }
                ],
            }
            q = query(raw="ROUSEMEBABY - SEXY MJ")
            cache_key = SearchCache.key_for(q)
            con = cache._connect()
            try:
                con.execute(
                    """
                    INSERT OR REPLACE INTO search_cache
                        (cache_key, created_at, status, reason, decision_reason, payload)
                    VALUES (?, strftime('%s','now'), ?, ?, ?, ?)
                    """,
                    (cache_key, None, None, "cache_hit", json.dumps(payload, ensure_ascii=False)),
                )
                con.commit()
            finally:
                con.close()

            decision = asyncio.run(SearchOrchestrator([], cache=cache).search(q))

        self.assertTrue(decision.from_cache)
        self.assertIsNotNone(decision.selected_candidate)
        self.assertEqual(decision.selected_candidate.raw_title, "SEXY MJ")
        self.assertEqual(decision.selected_candidate.raw_display_title, "ROUSEMEBABY - SEXY MJ")
        self.assertGreaterEqual(float(decision.selected_candidate.score or 0.0), quality.OK_SCORE_THRESHOLD)


if __name__ == "__main__":
    unittest.main()
