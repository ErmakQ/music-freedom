from __future__ import annotations

import asyncio
from typing import Iterable

import quality as quality_layer

from .base import ProviderBase
from .cache import SearchCache
from .models import ProviderResult, SearchCandidate, SearchDecision, TrackQuery


class SearchOrchestrator:
    def __init__(
        self,
        providers: Iterable[ProviderBase],
        *,
        selenium_provider: ProviderBase | None = None,
        cache: SearchCache | None = None,
        http_concurrency: int = 30,
        early_stop_min_candidates: int = 3,
        enable_selenium_fallback: bool = False,
    ) -> None:
        self.providers = list(providers)
        self.selenium_provider = selenium_provider
        self.cache = cache
        self.http_concurrency = max(1, int(http_concurrency))
        self.early_stop_min_candidates = max(1, int(early_stop_min_candidates))
        self.enable_selenium_fallback = enable_selenium_fallback

    async def search(self, track: TrackQuery) -> SearchDecision:
        cache_entry = self.cache.get_entry(track) if self.cache else None
        if cache_entry:
            decision = self._decision_from_cached(track, cache_entry["payload"])
            decision.from_cache = True
            decision.cache_key = cache_entry.get("cache_key")
            decision.cache_hit_type = cache_entry.get("cache_hit_type")
            return decision

        http_results = await self._run_http_providers(track)
        candidates = self._dedupe_candidates(
            candidate for result in http_results for candidate in result.candidates
        )
        decision = self._make_decision(
            track,
            candidates,
            http_results,
            decision_reason="http_providers",
        )

        if self._can_early_stop(decision):
            decision.decision_reason = "early_stop_http_high_confidence"
            decision.skipped_selenium_reason = "enough_http_candidates"
            if self.cache:
                self.cache.put(decision)
            return decision

        if (
            self.enable_selenium_fallback
            and self.selenium_provider is not None
            and not self._has_enough_http_candidates(decision)
        ):
            selenium_result = await self.selenium_provider.search(track)
            all_results = [*http_results, selenium_result]
            candidates = self._dedupe_candidates(
                candidate for result in all_results for candidate in result.candidates
            )
            decision = self._make_decision(
                track,
                candidates,
                all_results,
                decision_reason="http_plus_selenium_fallback",
            )
            decision.skipped_selenium_reason = None
        elif self.selenium_provider is None and self.enable_selenium_fallback:
            decision.skipped_selenium_reason = "selenium_provider_unavailable"
            if decision.reason == quality_layer.STATUS_NOT_FOUND:
                decision.decision_reason = "http_only_not_found_selenium_unavailable"
            else:
                decision.decision_reason = "http_only_selenium_unavailable"
        elif not self.enable_selenium_fallback:
            decision.skipped_selenium_reason = "selenium_disabled"
            if decision.reason == quality_layer.STATUS_NOT_FOUND:
                decision.decision_reason = "http_only_not_found_selenium_disabled"
            else:
                decision.decision_reason = "http_only_selenium_disabled"
        else:
            decision.skipped_selenium_reason = "enough_http_candidates"

        if self.cache:
            self.cache.put(decision)
        return decision

    async def _run_http_providers(self, track: TrackQuery) -> list[ProviderResult]:
        sem = asyncio.Semaphore(self.http_concurrency)

        async def _run(provider: ProviderBase) -> ProviderResult:
            async with sem:
                try:
                    return await provider.search(track)
                except Exception as exc:
                    return ProviderResult(
                        provider=getattr(provider, "name", provider.__class__.__name__),
                        candidates=[],
                        status="ERROR",
                        reason=f"{type(exc).__name__}: {exc}",
                    )

        return await asyncio.gather(*[_run(provider) for provider in self.providers])

    @staticmethod
    def _dedupe_candidates(candidates: Iterable[SearchCandidate]) -> list[SearchCandidate]:
        out: list[SearchCandidate] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = candidate.key()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(candidate)
        return out

    def _make_decision(
        self,
        track: TrackQuery,
        search_candidates: list[SearchCandidate],
        provider_results: list[ProviderResult],
        *,
        decision_reason: str,
    ) -> SearchDecision:
        quality_candidates = [self._to_quality_candidate(candidate, track) for candidate in search_candidates]
        selected, reason, ranked = quality_layer.select_best_candidate(track.raw, quality_candidates)
        downloadable = [
            candidate
            for candidate in ranked
            if not candidate.hard_reject
            and candidate.download_url
            and candidate.score >= quality_layer.LOW_CONFIDENCE_THRESHOLD
        ]
        status = None if downloadable else (reason or quality_layer.STATUS_NOT_FOUND)
        provider_reason = "; ".join(
            f"{result.provider}:{result.reason or len(result.candidates)}" for result in provider_results
        )
        final_reason = None if downloadable else (
            provider_reason if status == quality_layer.STATUS_NOT_FOUND else status
        )
        for candidate in ranked:
            setattr(candidate, "decision_reason", getattr(candidate, "decision_reason", None) or decision_reason)
        return SearchDecision(
            track=track,
            candidates=ranked,
            selected_candidate=selected or (ranked[0] if ranked else None),
            status=status,
            reason=final_reason,
            decision_reason=decision_reason,
            provider_results=provider_results,
        )

    @staticmethod
    def _to_quality_candidate(candidate: SearchCandidate, track: TrackQuery) -> quality_layer.TrackCandidate:
        q = quality_layer.TrackCandidate(
            provider=candidate.provider,
            input_track=candidate.input_track,
            raw_artist=candidate.raw_artist,
            raw_title=candidate.raw_title,
            raw_display_title=candidate.raw_display_title,
            duration_sec=candidate.duration_sec or track.expected_duration_sec,
            normalized_artist=quality_layer.normalize_track_text(candidate.raw_artist) if candidate.raw_artist else None,
            normalized_title=quality_layer.normalize_track_text(candidate.raw_title) if candidate.raw_title else None,
            normalized_display_title=quality_layer.normalize_track_text(candidate.raw_display_title or ""),
            source_url=candidate.source_url,
            download_url=candidate.download_url,
            referer=candidate.referer,
            cookie_header=candidate.cookie_header,
            search_query=candidate.search_query,
        )
        setattr(q, "decision_reason", candidate.decision_reason)
        return q

    def _can_early_stop(self, decision: SearchDecision) -> bool:
        return self._has_enough_http_candidates(decision)

    def _has_enough_http_candidates(self, decision: SearchDecision) -> bool:
        if not decision.candidates:
            return False
        best = decision.candidates[0]
        if getattr(best, "hard_reject", False):
            return False
        return float(getattr(best, "score", 0.0) or 0.0) >= quality_layer.OK_SCORE_THRESHOLD

    def _decision_from_cached(self, track: TrackQuery, payload: dict) -> SearchDecision:
        from downloader_core import _display_title_from_download_url, parse_artist_title

        candidates = []
        for candidate in payload.get("candidates", []):
            if not isinstance(candidate, dict) or not candidate.get("download_url"):
                continue
            raw_display_title = candidate.get("raw_display_title") or ""
            raw_title = candidate.get("raw_title")
            raw_artist = candidate.get("raw_artist")
            download_url = candidate.get("download_url")
            referer = candidate.get("referer")
            if (
                isinstance(download_url, str)
                and "skysound" in str(candidate.get("provider") or "").lower()
                and ("skysound" in str(raw_display_title).lower() or "skysound" in str(raw_title or "").lower())
            ):
                cleaned_display = _display_title_from_download_url(download_url, referer)
                cleaned_artist, cleaned_title = parse_artist_title(cleaned_display)
                raw_display_title = cleaned_display
                raw_artist = cleaned_artist or raw_artist
                raw_title = cleaned_title or raw_title
            q = quality_layer.TrackCandidate(
                provider=candidate.get("provider") or "cache",
                input_track=candidate.get("input_track") or track.raw,
                raw_artist=raw_artist,
                raw_title=raw_title,
                raw_display_title=raw_display_title,
                duration_sec=candidate.get("duration_sec") or track.expected_duration_sec,
                normalized_artist=quality_layer.normalize_track_text(raw_artist) if raw_artist else candidate.get("normalized_artist"),
                normalized_title=quality_layer.normalize_track_text(raw_title) if raw_title else candidate.get("normalized_title"),
                normalized_display_title=quality_layer.normalize_track_text(raw_display_title or ""),
                source_url=candidate.get("source_url"),
                download_url=download_url,
                referer=referer,
                cookie_header=candidate.get("cookie_header") or "",
                search_query=candidate.get("search_query"),
            )
            setattr(q, "decision_reason", candidate.get("decision_reason") or "cache_hit")
            candidates.append(q)
        selected, reason, ranked = quality_layer.select_best_candidate(track.raw, candidates)
        status = payload.get("status")
        return SearchDecision(
            track=track,
            candidates=ranked,
            selected_candidate=selected or (ranked[0] if ranked else None),
            status=status,
            reason=payload.get("reason") or reason,
            decision_reason=payload.get("decision_reason") or "cache_hit",
            provider_results=[
                ProviderResult(
                    provider="cache",
                    candidates=[],
                    status=status or "OK",
                    reason="cache_hit",
                    from_cache=True,
                )
            ],
            from_cache=True,
            cache_hit_type="negative" if status else "positive",
            skipped_selenium_reason=payload.get("skipped_selenium_reason"),
        )
