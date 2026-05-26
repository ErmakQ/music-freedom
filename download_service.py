"""
Параметры DownloadConfig из переменных окружения (веб-API).
"""
from __future__ import annotations

import os

from downloader_core import DEFAULT_LMUSIC_ORIGIN, DEFAULT_SITE_ORIGIN, DownloadConfig


def load_download_config_from_env() -> DownloadConfig:
    site_origin = os.environ.get("SITE_ORIGIN", DEFAULT_SITE_ORIGIN).strip() or DEFAULT_SITE_ORIGIN
    lmusic_origin = os.environ.get("LMUSIC_ORIGIN", DEFAULT_LMUSIC_ORIGIN).strip() or DEFAULT_LMUSIC_ORIGIN
    lmusic_fallback = os.environ.get("LMUSIC_FALLBACK_ENABLED", "1").strip().lower() not in (
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
    return DownloadConfig(
        site_origin=site_origin,
        lmusic_origin=lmusic_origin,
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
        lmusic_fallback_enabled=lmusic_fallback,
    )
