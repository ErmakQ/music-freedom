"""
Параметры DownloadConfig из переменных окружения (веб-API).
"""
from __future__ import annotations

import os
from pathlib import Path

from downloader_core import DEFAULT_LMUSIC_ORIGIN, DEFAULT_SITE_ORIGIN, DEFAULT_SKYSOUND_ORIGIN, DEFAULT_ZVYKI_ORIGIN, DownloadConfig


def load_download_config_from_env() -> DownloadConfig:
    site_origin = os.environ.get("SITE_ORIGIN", DEFAULT_SITE_ORIGIN).strip() or DEFAULT_SITE_ORIGIN
    lmusic_origin = os.environ.get("LMUSIC_ORIGIN", DEFAULT_LMUSIC_ORIGIN).strip() or DEFAULT_LMUSIC_ORIGIN
    skysound_origin = os.environ.get("SKYSOUND_ORIGIN", DEFAULT_SKYSOUND_ORIGIN).strip() or DEFAULT_SKYSOUND_ORIGIN
    zvyki_origin = os.environ.get("ZVYKI_ORIGIN", DEFAULT_ZVYKI_ORIGIN).strip() or DEFAULT_ZVYKI_ORIGIN
    lmusic_fallback = os.environ.get("LMUSIC_FALLBACK_ENABLED", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    zvyki_fallback = os.environ.get("ZVYKI_FALLBACK_ENABLED", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    skysound_fallback = os.environ.get("SKYSOUND_FALLBACK_ENABLED", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    headless = os.environ.get("HEADLESS", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    wait_sec = int(os.environ.get("WAIT_SEC", "25"))
    delay = float(os.environ.get("DELAY", "0.5"))
    parallel_dl = max(
        1,
        min(32, int(os.environ.get("DOWNLOAD_PARALLEL_WORKERS", "10"))),
    )
    resolve_parallel = max(
        1,
        min(8, int(os.environ.get("RESOLVE_PARALLEL_WORKERS", "4"))),
    )
    fetch_track_pages = os.environ.get("HITMO_FETCH_TRACK_PAGES", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    search_rounds = max(1, min(6, int(os.environ.get("SEARCH_LOAD_ROUNDS", "3"))))
    search_try_http = os.environ.get("SEARCH_TRY_HTTP_FIRST", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    search_translit = os.environ.get("SEARCH_TRANSLIT", "extra").strip().lower()
    if search_translit not in ("off", "replace", "extra"):
        search_translit = "off"
    http_search_tmo = float(os.environ.get("HTTP_SEARCH_TIMEOUT", "25"))
    enable_selenium = os.environ.get("ENABLE_SELENIUM_FALLBACK", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    search_cache_enabled = os.environ.get("SEARCH_CACHE_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    search_cache_ttl = int(os.environ.get("SEARCH_CACHE_TTL_DAYS", "14"))
    negative_cache_ttl_hours = int(os.environ.get("NEGATIVE_CACHE_TTL_HOURS", "12"))
    bypass_negative_cache_for_deep = os.environ.get(
        "BYPASS_NEGATIVE_CACHE_FOR_DEEP",
        "true",
    ).strip().lower() not in ("0", "false", "no")
    http_search_concurrency = max(1, min(100, int(os.environ.get("HTTP_SEARCH_CONCURRENCY", "30"))))
    download_concurrency = max(1, min(32, int(os.environ.get("DOWNLOAD_CONCURRENCY", str(parallel_dl)))))
    validation_concurrency = max(1, min(32, int(os.environ.get("VALIDATION_CONCURRENCY", "4"))))
    selenium_max_workers = max(0, min(4, int(os.environ.get("SELENIUM_MAX_WORKERS", "1"))))
    provider_timeout = float(os.environ.get("PROVIDER_TIMEOUT_SEC", "8"))
    search_mode = os.environ.get("SEARCH_MODE", "balanced").strip().lower()
    if search_mode not in ("fast", "balanced", "deep"):
        search_mode = "balanced"
    if search_mode == "deep":
        enable_selenium = True
    cache_path = os.environ.get(
        "SEARCH_CACHE_PATH",
        str(Path("downloaded_music") / "search_cache.sqlite3"),
    )
    return DownloadConfig(
        site_origin=site_origin,
        lmusic_origin=lmusic_origin,
        skysound_origin=skysound_origin,
        zvyki_origin=zvyki_origin,
        headless=headless,
        wait_sec=wait_sec,
        delay_sec=delay,
        parallel_download_workers=parallel_dl,
        resolve_parallel_workers=resolve_parallel,
        fetch_track_pages_when_search_has_mp3=fetch_track_pages,
        search_load_rounds=search_rounds,
        search_try_http_first=search_try_http,
        search_transliterate=search_translit,
        http_search_timeout=http_search_tmo,
        http_search_concurrency=http_search_concurrency,
        download_concurrency=download_concurrency,
        validation_parallel_workers=validation_concurrency,
        selenium_max_workers=selenium_max_workers,
        provider_timeout_sec=provider_timeout,
        search_mode=search_mode,
        enable_selenium_fallback=enable_selenium,
        search_cache_enabled=search_cache_enabled,
        search_cache_ttl_days=search_cache_ttl,
        negative_cache_ttl_hours=negative_cache_ttl_hours,
        bypass_negative_cache_for_deep=bypass_negative_cache_for_deep,
        search_cache_path=cache_path,
        lmusic_fallback_enabled=lmusic_fallback,
        skysound_fallback_enabled=skysound_fallback,
        zvyki_fallback_enabled=zvyki_fallback,
    )
