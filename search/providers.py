from __future__ import annotations

import asyncio
import http.cookiejar
import logging
import re
import urllib.parse
from typing import Any

import httpx

from .base import ProviderBase
from .models import ProviderResult, SearchCandidate, TrackQuery

log = logging.getLogger("playlist_search")


def _decode_response_text(resp: httpx.Response) -> str:
    if resp.encoding:
        return resp.text
    raw = resp.content
    for enc in ("utf-8", "utf-8-sig", "cp1251", "windows-1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _candidate_from_attempt(
    provider: str,
    track: TrackQuery,
    search_query: str,
    attempt: tuple[str, str, str],
    *,
    decision_reason: str,
) -> SearchCandidate:
    from downloader_core import _display_title_from_download_url, parse_artist_title

    download_url, referer, cookie_header = attempt
    display = _display_title_from_download_url(download_url, referer)
    raw_artist, raw_title = parse_artist_title(display)
    return SearchCandidate(
        provider=provider,
        input_track=track.raw,
        search_query=search_query,
        source_url=referer,
        download_url=download_url,
        referer=referer,
        cookie_header=cookie_header or "",
        raw_artist=raw_artist or None,
        raw_title=raw_title or None,
        raw_display_title=display,
        decision_reason=decision_reason,
    )


class HitmoHTTPProvider(ProviderBase):
    name = "hitmo:http"

    def __init__(self, cfg: Any, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self.client = client

    async def search(self, track: TrackQuery) -> ProviderResult:
        from downloader_core import (
            _extract_get_music_mp3_urls,
            http_cookie_header_for_site,
            normalize_hitmotop_url,
            score_mp3_url_path,
        )

        headers = {
            "User-Agent": self.cfg.chrome_ua,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }
        own_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=self.cfg.provider_timeout_sec, follow_redirects=True)
        candidates: list[SearchCandidate] = []
        seen: set[str] = set()
        try:
            for query in track.generated_queries:
                encoded = urllib.parse.quote(query, safe="")
                search_url = f"{self.cfg.site_origin.rstrip('/')}/search?q={encoded}"
                try:
                    resp = await client.get(search_url, headers=headers)
                    resp.raise_for_status()
                except Exception as exc:
                    log.debug("Hitmo HTTP miss %s: %s", query, exc)
                    continue
                html = _decode_response_text(resp)
                urls = _extract_get_music_mp3_urls(html, self.cfg.site_origin)
                urls.sort(
                    key=lambda u: score_mp3_url_path(u, track.raw, track.artist, track.title),
                    reverse=True,
                )
                cookie_header = ""
                try:
                    jar = http.cookiejar.CookieJar()
                    cookie_header = http_cookie_header_for_site(jar, self.cfg.site_origin)
                except Exception:
                    cookie_header = ""
                for url in urls[:5]:
                    normalized = normalize_hitmotop_url(url)
                    if normalized in seen:
                        continue
                    seen.add(normalized)
                    candidates.append(
                        _candidate_from_attempt(
                            self.name,
                            track,
                            query,
                            (normalized, search_url, cookie_header),
                            decision_reason="hitmo_http_search",
                        )
                    )
                if len(candidates) >= 5:
                    break
            return ProviderResult(self.name, candidates, reason=None if candidates else "miss")
        finally:
            if own_client:
                await client.aclose()


class ZvykiHTTPProvider(ProviderBase):
    name = "zvyki:http"

    def __init__(self, cfg: Any, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self.client = client

    async def search(self, track: TrackQuery) -> ProviderResult:
        from downloader_core import (
            _extract_zvyki_audio_urls,
            _extract_zvyki_track_page_urls,
            _zvyki_search_urls,
            score_mp3_url_path,
        )

        if not self.cfg.zvyki_fallback_enabled:
            return ProviderResult(self.name, [], status="DISABLED", reason="disabled")

        headers = {
            "User-Agent": self.cfg.chrome_ua,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Referer": self.cfg.zvyki_origin.rstrip("/") + "/",
        }
        own_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=self.cfg.provider_timeout_sec, follow_redirects=True)
        candidates: list[SearchCandidate] = []
        seen: set[str] = set()
        try:
            for query in track.generated_queries[:2]:
                for search_url in _zvyki_search_urls(self.cfg, query):
                    try:
                        resp = await client.get(search_url, headers=headers)
                        resp.raise_for_status()
                    except Exception:
                        continue
                    html = _decode_response_text(resp)
                    final_url = str(resp.url)
                    audio_urls = _extract_zvyki_audio_urls(html, self.cfg.zvyki_origin)
                    if not audio_urls:
                        track_urls = _extract_zvyki_track_page_urls(html, self.cfg.zvyki_origin)
                        track_urls.sort(
                            key=lambda u: score_mp3_url_path(u, track.raw, track.artist, track.title),
                            reverse=True,
                        )
                        for track_url in track_urls[:3]:
                            try:
                                page_resp = await client.get(track_url, headers=headers)
                                page_resp.raise_for_status()
                            except Exception:
                                continue
                            page_html = _decode_response_text(page_resp)
                            for audio_url in _extract_zvyki_audio_urls(page_html, self.cfg.zvyki_origin):
                                if score_mp3_url_path(audio_url, track.raw, track.artist, track.title) < 40:
                                    continue
                                if audio_url in seen:
                                    continue
                                seen.add(audio_url)
                                candidates.append(
                                    _candidate_from_attempt(
                                        self.name,
                                        track,
                                        query,
                                        (audio_url, str(page_resp.url), ""),
                                        decision_reason="zvyki_track_page",
                                    )
                                )
                                if len(candidates) >= 5:
                                    return ProviderResult(self.name, candidates)
                    else:
                        audio_urls.sort(
                            key=lambda u: score_mp3_url_path(u, track.raw, track.artist, track.title),
                            reverse=True,
                        )
                        for audio_url in audio_urls[:5]:
                            if score_mp3_url_path(audio_url, track.raw, track.artist, track.title) < 40:
                                continue
                            if audio_url in seen:
                                continue
                            seen.add(audio_url)
                            candidates.append(
                                _candidate_from_attempt(
                                    self.name,
                                    track,
                                    query,
                                    (audio_url, final_url, ""),
                                    decision_reason="zvyki_search_page",
                                )
                            )
                    if candidates:
                        return ProviderResult(self.name, candidates)
            return ProviderResult(self.name, candidates, reason=None if candidates else "miss")
        finally:
            if own_client:
                await client.aclose()


class LMusicHTTPProvider(ProviderBase):
    name = "lmusic:http"

    def __init__(self, cfg: Any, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self.client = client

    async def search(self, track: TrackQuery) -> ProviderResult:
        if not self.cfg.lmusic_fallback_enabled:
            return ProviderResult(self.name, [], status="DISABLED", reason="disabled")
        # LMusic in the old pipeline was Selenium-only. This HTTP adapter is a conservative
        # best-effort parser for direct links already present in public HTML pages.
        from downloader_core import _extract_lmusic_download_urls, _extract_lmusic_track_urls_from_html

        own_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=self.cfg.provider_timeout_sec, follow_redirects=True)
        candidates: list[SearchCandidate] = []
        seen: set[str] = set()
        try:
            for query in track.generated_queries[:2]:
                encoded = urllib.parse.quote_plus(query)
                urls = [
                    f"{self.cfg.lmusic_origin.rstrip('/')}/?s={encoded}",
                    f"{self.cfg.lmusic_origin.rstrip('/')}/search/{encoded}",
                ]
                for url in urls:
                    try:
                        resp = await client.get(url, headers={"User-Agent": self.cfg.chrome_ua})
                        resp.raise_for_status()
                    except Exception:
                        continue
                    html = _decode_response_text(resp)
                    download_urls = _extract_lmusic_download_urls(html, self.cfg.lmusic_origin)
                    if not download_urls:
                        for track_url in _extract_lmusic_track_urls_from_html(html, self.cfg.lmusic_origin)[:3]:
                            try:
                                page_resp = await client.get(track_url, headers={"User-Agent": self.cfg.chrome_ua})
                                page_resp.raise_for_status()
                            except Exception:
                                continue
                            download_urls.extend(
                                _extract_lmusic_download_urls(
                                    _decode_response_text(page_resp),
                                    self.cfg.lmusic_origin,
                                )
                            )
                    for download_url in download_urls[:5]:
                        if download_url in seen:
                            continue
                        seen.add(download_url)
                        candidate = _candidate_from_attempt(
                            self.name,
                            track,
                            query,
                            (download_url, str(resp.url), ""),
                            decision_reason="lmusic_http_best_effort",
                        )
                        if (candidate.raw_display_title or "").strip().lower() in {"track", "download", "file", "audio"}:
                            continue
                        candidates.append(candidate)
                    if candidates:
                        return ProviderResult(self.name, candidates)
            return ProviderResult(self.name, candidates, reason=None if candidates else "miss")
        finally:
            if own_client:
                await client.aclose()


class SeleniumFallbackProvider(ProviderBase):
    name = "selenium:fallback"

    def __init__(self, cfg: Any, driver: Any, wait: Any) -> None:
        self.cfg = cfg
        self.driver = driver
        self.wait = wait

    async def search(self, track: TrackQuery) -> ProviderResult:
        def _run() -> ProviderResult:
            from downloader_core import _find_lmusic_plan_via_selenium, _find_plan_via_selenium

            candidates: list[SearchCandidate] = []
            seen: set[str] = set()
            for query in track.generated_queries:
                attempts = _find_plan_via_selenium(
                    self.driver,
                    self.wait,
                    self.cfg,
                    track.raw,
                    query,
                )
                if not attempts:
                    attempts = _find_lmusic_plan_via_selenium(
                        self.driver,
                        self.wait,
                        self.cfg,
                        track.raw,
                        query,
                    )
                for attempt in attempts:
                    key = attempt[0]
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(
                        _candidate_from_attempt(
                            self.name,
                            track,
                            query,
                            attempt,
                            decision_reason="selenium_fallback",
                        )
                    )
                if candidates:
                    break
            return ProviderResult(self.name, candidates, reason=None if candidates else "miss")

        return await asyncio.to_thread(_run)
