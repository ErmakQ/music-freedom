"""
Ядро скачивания по плейлисту (Selenium + HTTP). Используется web- и CLI-режимами проекта.

Схема: по возможности сначала HTTP GET страницы поиска и разбор ссылок /get/music/*.mp3 (без Chrome);
если не вышло — Selenium. Запрос в поиске можно строить в транслите (кириллица → латиница).
Фаза 2: параллельные HTTP-скачивания файлов.

ChromeDriver: при старте определяется версия Chrome, по официальному манифесту Chrome for Testing
скачивается подходящий chromedriver в кэш (см. CHROMEDRIVER_CACHE_DIR). Если не вышло — запасной
вариант chromedriver-autoinstaller. Вручную: CHROMEDRIVER_PATH или CHROMEDRIVER_SKIP_AUTOINSTALL=1.
"""
from __future__ import annotations

import asyncio
import http.cookiejar
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from typing import Callable

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

import quality as quality_layer
from playlist_utils import extract_playlist_duration_sec, normalize_playlist_line

_log_write_lock = threading.Lock()
_chromedriver_install_lock = threading.Lock()
_chromedriver_install_ok = False
_chromedriver_install_error: BaseException | None = None
_cft_milestones_cache: dict | None = None
log = logging.getLogger("playlist_dl")

CFT_MILESTONE_JSON_URL = os.environ.get(
    "CHROMEDRIVER_CFT_JSON_URL",
    "https://googlechromelabs.github.io/chrome-for-testing/latest-versions-per-milestone-with-downloads.json",
).strip()
_CFT_FETCH_TIMEOUT_SEC = 180
_MAX_DOWNLOAD_BYTES = max(
    0, int(os.environ.get("MAX_DOWNLOAD_BYTES", "0") or "0")
)  # 0 = без лимита


def _chromedriver_cache_root() -> Path:
    raw = os.environ.get("CHROMEDRIVER_CACHE_DIR", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".cache" / "muzik_maks" / "chromedriver"


def _cft_download_platform() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        if machine in ("arm64", "aarch64"):
            return "win64"
        return "win64" if sys.maxsize > 2**32 else "win32"
    if system == "darwin":
        return "mac-arm64" if machine in ("arm64", "aarch64") else "mac-x64"
    return "linux64"


def _installed_chrome_version() -> str | None:
    if sys.platform == "win32":
        try:
            import winreg
        except ImportError:
            winreg = None  # type: ignore[assignment]
        if winreg is not None:
            for hive, subkey in (
                (winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon"),
                (winreg.HKEY_LOCAL_MACHINE, r"Software\Google\Chrome\BLBeacon"),
                (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Google\Chrome\BLBeacon"),
            ):
                try:
                    with winreg.OpenKey(hive, subkey) as k:
                        ver, _ = winreg.QueryValueEx(k, "version")
                        s = str(ver).strip()
                        if s:
                            return s
                except OSError:
                    continue
        roots = (
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        )
        for root in roots:
            if not root:
                continue
            candidate = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if candidate.is_file():
                try:
                    out = subprocess.run(
                        [str(candidate), "--version"],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        creationflags=subprocess.CREATE_NO_WINDOW
                        if hasattr(subprocess, "CREATE_NO_WINDOW")
                        else 0,
                    )
                    line = (out.stdout or out.stderr or "").strip()
                    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
                    if m:
                        return m.group(1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                break
        return None
    if sys.platform == "darwin":
        chrome = Path(
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        )
        if chrome.is_file():
            try:
                out = subprocess.run(
                    [str(chrome), "--version"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                line = (out.stdout or out.stderr or "").strip()
                m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
                if m:
                    return m.group(1)
            except (OSError, subprocess.TimeoutExpired):
                pass
        return None
    for cmd in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        try:
            out = subprocess.run(
                [cmd, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            line = (out.stdout or out.stderr or "").strip()
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
            if m:
                return m.group(1)
        except (OSError, subprocess.TimeoutExpired, FileNotFoundError):
            continue
    return None


def _fetch_url_bytes(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "muzik_maks/chromedriver-setup"},
    )
    with urllib.request.urlopen(req, timeout=_CFT_FETCH_TIMEOUT_SEC) as resp:
        return resp.read()


def _load_cft_milestones() -> dict:
    global _cft_milestones_cache
    if _cft_milestones_cache is not None:
        return _cft_milestones_cache
    raw = _fetch_url_bytes(CFT_MILESTONE_JSON_URL)
    data = json.loads(raw.decode("utf-8"))
    milestones = data.get("milestones")
    if not isinstance(milestones, dict):
        raise RuntimeError("Chrome for Testing: в JSON нет поля milestones")
    _cft_milestones_cache = milestones
    return milestones


def _chromedriver_zip_url_for_chrome(
    chrome_version: str, milestones: dict
) -> tuple[str, str, str]:
    parts = chrome_version.split(".")
    if not parts or not parts[0].isdigit():
        raise RuntimeError(f"Некорректная версия Chrome: {chrome_version!r}")
    major = parts[0]
    entry = milestones.get(major)
    if not isinstance(entry, dict):
        keys = sorted((int(k) for k in milestones if str(k).isdigit()), reverse=True)
        hint = f"известные майлстоуны: {keys[:8]}…" if keys else "нет данных"
        raise RuntimeError(
            f"Chrome for Testing: нет сборки для майлстоуна {major} ({hint}). "
            "Обновите Chrome или задайте CHROMEDRIVER_PATH."
        )
    cft_version = str(entry.get("version") or "").strip()
    if not cft_version:
        raise RuntimeError("Chrome for Testing: пустая version в манифесте")
    plat = _cft_download_platform()
    downloads = entry.get("downloads") or {}
    cds = downloads.get("chromedriver")
    if not cds:
        raise RuntimeError(
            f"Chrome for Testing: для майлстоуна {major} нет chromedriver в манифесте "
            "(очень старый Chrome?). Задайте CHROMEDRIVER_PATH или обновите браузер."
        )
    url = None
    for item in cds:
        if isinstance(item, dict) and item.get("platform") == plat:
            url = item.get("url")
            break
    if not url:
        raise RuntimeError(
            f"Chrome for Testing: нет chromedriver для платформы {plat} (версия {cft_version})"
        )
    return str(url), cft_version, plat


def _find_chromedriver_binary(extract_root: Path) -> Path | None:
    for name in ("chromedriver.exe", "chromedriver"):
        for p in extract_root.rglob(name):
            if p.is_file():
                return p
    return None


def _download_chromedriver_via_cft() -> Path:
    chrome_ver = _installed_chrome_version()
    if not chrome_ver:
        raise RuntimeError(
            "Не удалось определить версию Google Chrome (реестр / chrome --version). "
            "Установите Chrome или укажите CHROMEDRIVER_PATH."
        )
    milestones = _load_cft_milestones()
    zip_url, cft_version, plat = _chromedriver_zip_url_for_chrome(chrome_ver, milestones)
    cache_root = _chromedriver_cache_root()
    dest_dir = cache_root / f"cft-{cft_version}-{plat}"
    binary_name = "chromedriver.exe" if plat.startswith("win") else "chromedriver"
    dest_bin = dest_dir / binary_name
    if dest_bin.is_file():
        log.info(
            "ChromeDriver из кэша: %s (Chrome %s, сборка %s)",
            dest_bin,
            chrome_ver,
            cft_version,
        )
        return dest_bin

    dest_dir.mkdir(parents=True, exist_ok=True)
    log.info(
        "Качаю ChromeDriver %s для Chrome %s (%s)…",
        cft_version,
        chrome_ver,
        plat,
    )
    zip_bytes = _fetch_url_bytes(zip_url)
    with tempfile.TemporaryDirectory(prefix="muzik_cft_") as tmp:
        zpath = Path(tmp) / "chromedriver.zip"
        zpath.write_bytes(zip_bytes)
        extract_root = Path(tmp) / "extracted"
        extract_root.mkdir()
        with zipfile.ZipFile(zpath, "r") as zf:
            zf.extractall(extract_root)
        found = _find_chromedriver_binary(extract_root)
        if not found:
            raise RuntimeError("В архиве Chrome for Testing не найден chromedriver")
        shutil.copy2(found, dest_bin)
    if not sys.platform.startswith("win"):
        mode = dest_bin.stat().st_mode
        dest_bin.chmod(mode | 0o111)
    log.info("ChromeDriver сохранён: %s", dest_bin)
    return dest_bin


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def ensure_chromedriver_installed() -> None:
    """
    Один раз на процесс (под lock): Chrome for Testing → кэш, иначе chromedriver-autoinstaller.
    """
    global _chromedriver_install_ok, _chromedriver_install_error
    if _chromedriver_install_ok:
        return
    if _chromedriver_install_error is not None:
        raise _chromedriver_install_error
    if os.environ.get("CHROMEDRIVER_PATH", "").strip():
        _chromedriver_install_ok = True
        return
    if _truthy_env("CHROMEDRIVER_SKIP_AUTOINSTALL"):
        _chromedriver_install_ok = True
        log.info("CHROMEDRIVER_SKIP_AUTOINSTALL: пропускаю загрузку, ожидаю chromedriver в PATH")
        return
    with _chromedriver_install_lock:
        if _chromedriver_install_ok:
            return
        if _chromedriver_install_error is not None:
            raise _chromedriver_install_error
        if not _truthy_env("CHROMEDRIVER_SKIP_CFT"):
            try:
                path = _download_chromedriver_via_cft()
                os.environ["CHROMEDRIVER_PATH"] = str(path)
                _chromedriver_install_ok = True
                return
            except Exception as cft_err:
                log.warning(
                    "Chrome for Testing не сработал (%s), пробую chromedriver-autoinstaller",
                    cft_err,
                )
        try:
            from chromedriver_autoinstaller import install as _cd_install

            try:
                _cd_install()
            except Exception:
                _cd_install(no_ssl=True)
        except Exception as e:
            _chromedriver_install_error = RuntimeError(
                "Не удалось получить ChromeDriver: ни Chrome for Testing, ни chromedriver-autoinstaller. "
                "Проверьте сеть, VPN и файервол. Скачайте драйвер вручную с "
                "googlechromelabs.github.io/chrome-for-testing/, укажите CHROMEDRIVER_PATH "
                "или положите chromedriver в PATH и CHROMEDRIVER_SKIP_AUTOINSTALL=1. "
                f"Исходная ошибка: {e!s}"
            )
            log.error("%s", _chromedriver_install_error)
            raise _chromedriver_install_error from e
        _chromedriver_install_ok = True
        log.info("Chromedriver autoinstall: готово")


def _track_preview(raw: str, max_len: int = 72) -> str:
    s = raw.replace("\n", " ").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


_WIN_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_SITE_ORIGIN = "https://eu.hitmoz.com"
DEFAULT_LMUSIC_ORIGIN = "https://lmusic.kz"
DEFAULT_ZVYKI_ORIGIN = "https://zvyki.com"
DEFAULT_MP3_MIRROR_HOSTS = (
    "eu.hitmoz.com",
    "eu.hitmotop.com",
    "rus.hitmotop.com",
)


@dataclass
class DownloadConfig:
    site_origin: str = DEFAULT_SITE_ORIGIN
    lmusic_origin: str = DEFAULT_LMUSIC_ORIGIN
    zvyki_origin: str = DEFAULT_ZVYKI_ORIGIN
    headless: bool = True
    wait_sec: int = 25
    delay_sec: float = 0.5
    chrome_ua: str = CHROME_UA
    # Сколько Chrome параллельно на фазе 1 (поиск ссылок). 1 = как раньше, один браузер по очереди.
    resolve_parallel_workers: int = 4
    # Если на выдаче поиска уже есть mp3-ссылки, не ходить по страницам треков (сильно быстрее).
    # True — старое поведение: всегда добавлять запасные URL со страниц треков.
    fetch_track_pages_when_search_has_mp3: bool = False
    # Параметры ожидания выдачи поиска (меньше — быстрее, но выше риск ложного «нет ссылок»)
    search_load_rounds: int = 3
    search_scroll_sleep_start: float = 0.35
    search_scroll_sleep_step: float = 0.2
    search_refresh_sleep: float = 0.75
    # Сначала попытка найти ссылки на странице поиска через HTTP (часто без Selenium)
    search_try_http_first: bool = True
    http_search_timeout: float = 25.0
    # off | replace | extra — транслит кириллицы в строке поиска (extra: сначала как в плейлисте, потом транслит)
    search_transliterate: str = "off"
    # После поиска ссылок в браузере — сколько параллельных HTTP-скачиваний (1 = по очереди)
    parallel_download_workers: int = 10
    validation_parallel_workers: int = 4
    http_search_concurrency: int = 30
    download_concurrency: int = 5
    selenium_max_workers: int = 1
    provider_timeout_sec: float = 8.0
    search_mode: str = "balanced"
    enable_selenium_fallback: bool = False
    search_cache_enabled: bool = True
    search_cache_ttl_days: int = 14
    negative_cache_ttl_hours: int = 12
    bypass_negative_cache_for_deep: bool = True
    search_cache_path: str = ""
    # Fallback-поиск на lmusic.kz для треков, которые не нашлись на основном сайте.
    lmusic_fallback_enabled: bool = True
    zvyki_fallback_enabled: bool = True


_STOP_WORKER = object()

# Если план нашёлся быстро (обычно HTTP без Chrome) — не ждём полный delay_sec между треками.
_PHASE1_FAST_RESOLVE_SEC = 2.0


def _sleep_after_phase1_track(cfg: DownloadConfig, elapsed_sec: float) -> None:
    if elapsed_sec < _PHASE1_FAST_RESOLVE_SEC:
        time.sleep(min(cfg.delay_sec, 0.25))
    else:
        time.sleep(cfg.delay_sec)


@dataclass
class ResolvedTrack:
    """
    План скачивания: упорядоченные попытки (url, referer, cookie), как в старом цикле
    по кандидатам со страницы поиска и со страниц треков.
    """

    playlist_index: int
    raw_song_query: str
    attempts: list[tuple[str, str, str]] = field(default_factory=list)
    candidates: list[quality_layer.TrackCandidate] = field(default_factory=list)
    requested_flags: list[str] = field(default_factory=list)
    generated_queries: list[str] = field(default_factory=list)
    expected_duration_sec: int | None = None


def sanitize_filename(name: str) -> str:
    name = _WIN_INVALID.sub("_", name)
    name = name.rstrip(" .")
    return name or "track"


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    for i in range(1, 1000):
        candidate = f"{base}_{i}{ext}"
        if not os.path.exists(candidate):
            return candidate
    return f"{base}_{int(time.time())}{ext}"


def normalize_hitmotop_url(url: str) -> str:
    if url.startswith("http://eu.hitmoz.com"):
        return "https://" + url[len("http://") :]
    return url


def _mirror_schemes_for_host(host: str, original_scheme: str) -> list[str]:
    original = (original_scheme or "").lower()
    if host == "eu.hitmotop.com":
        ordered = [original] if original in ("http", "https") else ["http"]
        for scheme in ("http", "https"):
            if scheme not in ordered:
                ordered.append(scheme)
        return ordered
    if host.startswith("rus."):
        ordered = [original] if original in ("http", "https") else ["http"]
        for scheme in ("http", "https"):
            if scheme not in ordered:
                ordered.append(scheme)
        return ordered
    ordered = [original] if original in ("http", "https") else ["https"]
    if "http" not in ordered:
        ordered.append("http")
    if "https" not in ordered:
        ordered.append("https")
    return ordered


def candidate_download_urls(download_url: str, site_origin: str) -> list[str]:
    parsed = urllib.parse.urlparse(normalize_hitmotop_url(download_url))
    if not parsed.scheme or not parsed.netloc or "/get/music/" not in parsed.path:
        return [normalize_hitmotop_url(download_url)]

    site_host = (urllib.parse.urlparse(site_origin).hostname or "").lower()
    configured_hosts = [
        h.strip().lower()
        for h in os.environ.get("MP3_MIRROR_HOSTS", "").split(",")
        if h.strip()
    ]
    ordered_hosts: list[str] = []
    for host in [parsed.netloc.lower(), site_host, *configured_hosts, *DEFAULT_MP3_MIRROR_HOSTS]:
        if host and host not in ordered_hosts:
            ordered_hosts.append(host)

    candidates: list[str] = []
    original_url = normalize_hitmotop_url(download_url)
    candidates.append(original_url)
    for host in ordered_hosts:
        for scheme in _mirror_schemes_for_host(host, parsed.scheme):
            mirror_url = urllib.parse.urlunparse(
                (scheme, host, parsed.path, parsed.params, parsed.query, parsed.fragment)
            )
            mirror_url = normalize_hitmotop_url(mirror_url)
            if mirror_url not in candidates:
                candidates.append(mirror_url)
    return candidates


def parse_artist_title(raw: str) -> tuple[str, str]:
    if " - " in raw:
        artist, title = raw.split(" - ", 1)
        return artist.strip(), title.strip()
    if "-" in raw:
        artist, title = raw.split("-", 1)
        return artist.strip(), title.strip()
    return raw.strip(), raw.strip()


def build_search_query(raw: str) -> str:
    return re.sub(r"\s+", " ", raw.strip())


_CYR_TO_LAT_LOWER = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "yo",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
    "і": "i",
    "ї": "yi",
    "є": "ye",
    "ґ": "g",
}


def _has_cyrillic(s: str) -> bool:
    return bool(re.search(r"[\u0400-\u04FF]", s))


def transliterate_ru_to_latin(text: str) -> str:
    """Грубый транслит кириллицы (ру/часть укр.) в латиницу для строки поиска; латиница в строке не трогается."""
    out: list[str] = []
    for ch in text:
        lo = ch.lower()
        if lo in _CYR_TO_LAT_LOWER:
            out.append(_CYR_TO_LAT_LOWER[lo])
        else:
            out.append(ch)
    return "".join(out)


def search_query_variants(cfg: DownloadConfig, raw: str) -> list[str]:
    """Варианты строки для параметра q= (оригинал и/или транслит)."""
    base_queries = quality_layer.generate_search_queries(raw) or [build_search_query(raw)]
    mode = (cfg.search_transliterate or "off").strip().lower()
    if mode not in ("off", "replace", "extra"):
        mode = "off"
    out: list[str] = []

    def add(query: str) -> None:
        query = re.sub(r"\s+", " ", query.strip())
        if query and query.casefold() not in {q.casefold() for q in out}:
            out.append(query)

    for base in base_queries:
        if mode == "off":
            add(base)
            continue
        tr = transliterate_ru_to_latin(base) if _has_cyrillic(base) else base
        tr = re.sub(r"\s+", " ", tr).strip()
        if mode == "replace":
            add(tr if tr else base)
        elif mode == "extra":
            add(base)
            if tr and tr.casefold() != base.casefold():
                add(tr)
    return out[:12]


def _latin_tokens(s: str) -> set[str]:
    return {m.group(0).lower() for m in re.finditer(r"[a-zA-Z][a-zA-Z0-9]{2,}", s)}


def _row_context_text(anchor) -> str:
    cur = anchor
    for _ in range(10):
        try:
            cur = cur.find_element(By.XPATH, "./..")
            t = (cur.text or "").strip()
            if 15 <= len(t) <= 800:
                return t.lower()
        except Exception:
            break
    return ""


def score_mp3_candidate(
    href: str, anchor, raw: str, artist: str, title: str
) -> int:
    path = urllib.parse.unquote(urllib.parse.urlparse(href).path).lower()
    row = _row_context_text(anchor)
    score = 0
    tl = title.strip().lower()
    al = artist.strip().lower()
    if tl and len(tl) > 1 and tl in row:
        score += 80
    if al and len(al) > 2:
        if al in row:
            score += 70
        for part in re.split(r"[\s,]+", al):
            if len(part) > 2 and part in row:
                score += 25
    for tok in _latin_tokens(raw):
        if tok in path or tok in row:
            score += 18
    for tok in _latin_tokens(artist + " " + title):
        if tok in path:
            score += 12
    return score


def collect_mp3_candidates_from_search(driver) -> list[tuple[str, object]]:
    seen: set[str] = set()
    out: list[tuple[str, object]] = []
    for el in driver.find_elements(By.CSS_SELECTOR, "a.track__download-btn"):
        h = (el.get_attribute("href") or "").strip()
        if not h or "/get/music/" not in h:
            continue
        if not h.lower().endswith(".mp3"):
            continue
        h = normalize_hitmotop_url(h)
        if h not in seen:
            seen.add(h)
            out.append((h, el))
    return out


def collect_track_page_urls(driver, site_origin: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    origin = urllib.parse.urlparse(site_origin).netloc
    for el in driver.find_elements(By.CSS_SELECTOR, "a[href]"):
        h = (el.get_attribute("href") or "").strip().split("#")[0].rstrip("/")
        if not h or origin not in h:
            continue
        if h.endswith(".mp3") or "/get/music/" in h:
            continue
        if "/search" in h:
            continue
        if h == site_origin.rstrip("/") or h == f"https://{origin}":
            continue
        if h not in seen:
            seen.add(h)
            out.append(h)
        if len(out) >= 8:
            break
    return out[:3]


def collect_lmusic_track_page_urls(driver, site_origin: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    origin = urllib.parse.urlparse(site_origin).netloc
    for el in driver.find_elements(By.CSS_SELECTOR, "a[href]"):
        h = (el.get_attribute("href") or "").strip().split("#")[0].rstrip("/")
        if not h or origin not in h:
            continue
        if "/mp3/" not in h:
            continue
        if h not in seen:
            seen.add(h)
            out.append(h)
        if len(out) >= 8:
            break
    return out


def _extract_lmusic_download_urls(html: str, site_origin: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in re.finditer(
        r'https?://[^\s"\'<>]+/api/download/\d+',
        html,
        flags=re.I,
    ):
        u = m.group(0).rstrip(").,;\"'")
        if u not in seen:
            seen.add(u)
            out.append(u)
    for m in re.finditer(
        r'(["\'])(/api/download/\d+)\1',
        html,
        flags=re.I,
    ):
        u = urllib.parse.urljoin(site_origin, m.group(2))
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _extract_lmusic_track_urls_from_html(html: str, site_origin: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in re.finditer(
        r'https?://[^\s"\'<>]+/mp3/[^\s"\'<>]+',
        html,
        flags=re.I,
    ):
        u = m.group(0).rstrip(").,;\"'")
        if u not in seen:
            seen.add(u)
            out.append(u)
    for m in re.finditer(
        r'href\s*=\s*(["\'])(/mp3/\S+?)\1',
        html,
        flags=re.I,
    ):
        u = urllib.parse.urljoin(site_origin, m.group(2))
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _decode_html_bytes(raw_bytes: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp1251"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def _fetch_public_html(url: str, cfg: DownloadConfig) -> tuple[str, str, str] | None:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    headers = {
        "User-Agent": cfg.chrome_ua,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Referer": cfg.zvyki_origin.rstrip("/") + "/",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(req, timeout=cfg.http_search_timeout) as resp:
            final_url = resp.geturl()
            raw_bytes = resp.read()
    except Exception as exc:
        log.debug("Zvyki HTTP %s: %s", url, exc)
        return None
    return _decode_html_bytes(raw_bytes), final_url, http_cookie_header_for_site(jar, cfg.zvyki_origin)


def _extract_zvyki_audio_urls(html: str, site_origin: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    patterns = [
        r'https?://[^\s"\'<>]+\.mp3(?:\?[^\s"\'<>]*)?',
        r'(?:href|src|data-url|data-src)\s*=\s*(["\'])(/[^"\']+\.mp3(?:\?[^"\']*)?)\1',
        r'(?:href|data-url|data-src)\s*=\s*(["\'])(/[^"\']*(?:download|get)[^"\']*)\1',
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, html, flags=re.I):
            raw = m.group(0) if m.lastindex is None else m.group(m.lastindex)
            if raw in ("'", '"'):
                continue
            u = urllib.parse.urljoin(site_origin, raw).rstrip(").,;\"'")
            if u and u not in seen:
                seen.add(u)
                out.append(u)
    return out


def _extract_zvyki_track_page_urls(html: str, site_origin: str) -> list[str]:
    origin = urllib.parse.urlparse(site_origin).netloc.lower()
    seen: set[str] = set()
    out: list[str] = []
    for m in re.finditer(r'href\s*=\s*(["\'])([^"\']+)\1', html, flags=re.I):
        href = m.group(2).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        u = urllib.parse.urljoin(site_origin, href).split("#")[0].rstrip("/")
        parsed = urllib.parse.urlparse(u)
        if (parsed.netloc or "").lower() != origin:
            continue
        path = parsed.path.lower()
        if any(skip in path for skip in ("/search", "/privacy", "/contact", "/dmca")):
            continue
        if path in ("", "/") or path.endswith(".mp3"):
            continue
        if u not in seen:
            seen.add(u)
            out.append(u)
        if len(out) >= 8:
            break
    return out


def _zvyki_search_urls(cfg: DownloadConfig, song_query: str) -> list[str]:
    origin = cfg.zvyki_origin.rstrip("/")
    encoded = urllib.parse.quote(song_query, safe="")
    plus = urllib.parse.quote_plus(song_query)
    return [
        f"{origin}/search/?q={encoded}",
        f"{origin}/search?q={encoded}",
        f"{origin}/index.php?do=search&subaction=search&story={plus}",
    ]


def try_zvyki_http_plan(
    cfg: DownloadConfig,
    raw_song_query: str,
    song_query: str,
    artist: str,
    title: str,
) -> list[tuple[str, str, str]]:
    if not cfg.zvyki_fallback_enabled:
        return []

    attempts: list[tuple[str, str, str]] = []
    seen_urls: set[str] = set()
    for search_url in _zvyki_search_urls(cfg, song_query):
        fetched = _fetch_public_html(search_url, cfg)
        if not fetched:
            continue
        html, final_url, cookie_header = fetched
        audio_urls = _extract_zvyki_audio_urls(html, cfg.zvyki_origin)
        if not audio_urls:
            track_urls = _extract_zvyki_track_page_urls(html, cfg.zvyki_origin)
            track_urls.sort(
                key=lambda u: score_mp3_url_path(u, raw_song_query, artist, title),
                reverse=True,
            )
            for track_url in track_urls[:5]:
                page = _fetch_public_html(track_url, cfg)
                if not page:
                    continue
                page_html, page_url, page_cookie = page
                scored_audio = [
                    (audio_url, score_mp3_url_path(audio_url, raw_song_query, artist, title))
                    for audio_url in _extract_zvyki_audio_urls(page_html, cfg.zvyki_origin)
                ]
                scored_audio.sort(key=lambda item: item[1], reverse=True)
                for audio_url, score in scored_audio:
                    if score < 40:
                        continue
                    if audio_url not in seen_urls:
                        seen_urls.add(audio_url)
                        attempts.append((audio_url, page_url, page_cookie or cookie_header))
                    if len(attempts) >= 5:
                        break
                if attempts:
                    break
        else:
            scored_audio = [
                (audio_url, score_mp3_url_path(audio_url, raw_song_query, artist, title))
                for audio_url in audio_urls
            ]
            scored_audio.sort(key=lambda item: item[1], reverse=True)
            for audio_url, score in scored_audio[:5]:
                if score < 40:
                    continue
                if audio_url not in seen_urls:
                    seen_urls.add(audio_url)
                    attempts.append((audio_url, final_url, cookie_header))
        if attempts:
            log.info(
                "Zvyki fallback: %s url для %s",
                len(attempts),
                _track_preview(song_query, 56),
            )
            return attempts
    return attempts


def _slugify_lmusic_segment(text: str) -> str:
    base = transliterate_ru_to_latin(text or "")
    base = base.lower()
    base = re.sub(r"[\"'`]+", "", base)
    base = re.sub(r"[^a-z0-9]+", "-", base)
    base = re.sub(r"-{2,}", "-", base).strip("-")
    return base


def _split_artist_candidates(artist: str) -> list[str]:
    base = re.sub(r"\s+", " ", (artist or "").strip())
    if not base:
        return []
    out: list[str] = [base]
    for part in re.split(
        r"\s*(?:,|&| feat\.?| ft\.?| x | и | and )\s*",
        base,
        flags=re.I,
    ):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out[:4]


def _lmusic_artist_page_urls(site_origin: str, artist: str) -> list[str]:
    base_origin = site_origin.rstrip("/")
    seen: set[str] = set()
    out: list[str] = []
    for candidate in _split_artist_candidates(artist):
        for slug in (
            _slugify_lmusic_segment(candidate),
            urllib.parse.quote(candidate.strip().lower()),
        ):
            if not slug:
                continue
            url = f"{base_origin}/artist/{slug}"
            if url not in seen:
                seen.add(url)
                out.append(url)
    return out


def _lmusic_search_queries(raw_song_query: str, artist: str, title: str) -> list[str]:
    out: list[str] = []
    for candidate in (
        raw_song_query,
        f"{artist} {title}".strip(),
        f"{title} {artist}".strip(),
        title.strip(),
        artist.strip(),
    ):
        candidate = build_search_query(candidate)
        if candidate and candidate not in out:
            out.append(candidate)
    return out


def _collect_lmusic_candidate_track_urls(
    driver,
    cfg: DownloadConfig,
    raw_song_query: str,
    artist: str,
    title: str,
) -> list[str]:
    track_urls = collect_lmusic_track_page_urls(driver, cfg.lmusic_origin)
    if not track_urls:
        track_urls = _extract_lmusic_track_urls_from_html(
            driver.page_source, cfg.lmusic_origin
        )
    if not track_urls:
        return []
    track_urls.sort(
        key=lambda u: score_mp3_url_path(u, raw_song_query, artist, title),
        reverse=True,
    )
    return track_urls


def _search_lmusic_via_input(
    driver,
    wait: WebDriverWait,
    cfg: DownloadConfig,
    search_query: str,
) -> list[str]:
    search_url = cfg.lmusic_origin.rstrip("/")
    driver.get(search_url)
    selectors = [
        "input[placeholder*='РљР°РєСѓСЋ']",
        "input[type='search']",
        "form input[type='text']",
        "input[type='text']",
    ]
    search_input = None
    for selector in selectors:
        try:
            search_input = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
            if search_input:
                break
        except TimeoutException:
            continue
    if search_input is None:
        return []

    try:
        search_input.clear()
    except Exception:
        pass
    search_input.send_keys(search_query)
    search_input.send_keys(Keys.ENTER)
    time.sleep(1.2)
    return _collect_lmusic_candidate_track_urls(
        driver,
        cfg,
        search_query,
        *parse_artist_title(search_query),
    )


def basename_from_mp3_url(download_url: str) -> str:
    path = urllib.parse.urlparse(download_url).path
    base = os.path.basename(path)
    return os.path.splitext(base)[0] if base else "track"


def _cookie_header(driver) -> str:
    parts = [f"{c['name']}={c['value']}" for c in driver.get_cookies()]
    return "; ".join(parts)


def http_cookie_header_for_site(jar: http.cookiejar.CookieJar, site_origin: str) -> str:
    host = (urllib.parse.urlparse(site_origin).hostname or "").lower()
    parts: list[str] = []
    try:
        for c in jar:
            dom = (c.domain or "").lstrip(".").lower()
            if not dom or host == dom or host.endswith("." + dom):
                parts.append(f"{c.name}={c.value}")
    except Exception:
        pass
    return "; ".join(parts)


def _extract_get_music_mp3_urls(html: str, site_origin: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in re.finditer(
        r'https?://[^\s"\'<>]+/get/music/[^\s"\'<>]+\.mp3',
        html,
        flags=re.I,
    ):
        u = m.group(0).rstrip(").,;\"'")
        u = normalize_hitmotop_url(u)
        if u not in seen:
            seen.add(u)
            out.append(u)
    for m in re.finditer(
        r'href\s*=\s*(["\'])(/get/music/\S+?\.mp3)\1',
        html,
        flags=re.I,
    ):
        u = normalize_hitmotop_url(urllib.parse.urljoin(site_origin, m.group(2)))
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def score_mp3_url_path(href: str, raw: str, artist: str, title: str) -> int:
    path = urllib.parse.unquote(urllib.parse.urlparse(href).path).lower()
    score = 0
    tl = title.strip().lower()
    al = artist.strip().lower()
    path_cmp = path.replace("-", "").replace("_", "").replace(" ", "")
    if tl and len(tl) > 1 and tl.replace(" ", "") in path_cmp:
        score += 70
    if al and len(al) > 2:
        if al.replace(" ", "") in path_cmp:
            score += 60
        for part in re.split(r"[\s,]+", al):
            if len(part) > 2 and part in path:
                score += 20
    for tok in _latin_tokens(raw):
        if tok in path:
            score += 18
    for tok in _latin_tokens(artist + " " + title):
        if tok in path:
            score += 12
    return score


def _display_title_from_download_url(download_url: str, referer: str | None = None) -> str:
    base = urllib.parse.unquote(basename_from_mp3_url(download_url))
    base = base.replace("+", " ").replace("_-_", " - ").replace("_", " ")
    base = re.sub(r"\b\d{4,}$", "", base).strip(" -_")
    base = re.sub(r"\s+", " ", base).strip()
    if base and base.lower() not in {"download", "file", "track", "audio"}:
        return base
    if referer:
        ref_path = urllib.parse.unquote(urllib.parse.urlparse(referer).path)
        ref_base = os.path.basename(ref_path.rstrip("/"))
        ref_base = ref_base.replace("-", " ").replace("_", " ")
        ref_base = re.sub(r"\s+", " ", ref_base).strip()
        if ref_base:
            return ref_base
    return base or "track"


def _candidate_from_attempt(
    provider: str,
    raw_song_query: str,
    song_query: str,
    attempt: tuple[str, str, str],
) -> quality_layer.TrackCandidate:
    download_url, referer, cookie_header = attempt
    display = _display_title_from_download_url(download_url, referer)
    raw_artist, raw_title = parse_artist_title(display)
    return quality_layer.TrackCandidate(
        provider=provider,
        input_track=raw_song_query,
        raw_artist=raw_artist or None,
        raw_title=raw_title or None,
        raw_display_title=display,
        normalized_artist=quality_layer.normalize_track_text(raw_artist) if raw_artist else None,
        normalized_title=quality_layer.normalize_track_text(raw_title) if raw_title else None,
        normalized_display_title=quality_layer.normalize_track_text(display),
        source_url=referer,
        download_url=download_url,
        referer=referer,
        cookie_header=cookie_header or "",
        search_query=song_query,
    )


def _append_candidates_from_attempts(
    out: list[quality_layer.TrackCandidate],
    provider: str,
    raw_song_query: str,
    song_query: str,
    attempts: list[tuple[str, str, str]],
    seen_urls: set[str],
) -> None:
    for attempt in attempts:
        download_url = attempt[0]
        if not download_url or download_url in seen_urls:
            continue
        seen_urls.add(download_url)
        out.append(_candidate_from_attempt(provider, raw_song_query, song_query, attempt))


def collect_quality_candidates_for_line(
    driver,
    wait: WebDriverWait,
    cfg: DownloadConfig,
    raw_song_query: str,
) -> tuple[list[quality_layer.TrackCandidate], str, list[str], list[str]]:
    artist, title = parse_artist_title(raw_song_query)
    requested_flags = sorted(quality_layer.detect_requested_version_flags(raw_song_query))
    generated_queries = search_query_variants(cfg, raw_song_query)
    diag: list[str] = []
    candidates: list[quality_layer.TrackCandidate] = []
    seen_urls: set[str] = set()

    for song_query in generated_queries:
        if cfg.search_try_http_first:
            hp = try_http_search_plan(
                cfg, raw_song_query, song_query, artist, title
            )
            if hp:
                _append_candidates_from_attempts(
                    candidates,
                    "hitmo:http",
                    raw_song_query,
                    song_query,
                    hp,
                    seen_urls,
                )
                if len(candidates) >= 3:
                    break
            else:
                diag.append(f"hitmo:http:{song_query}=miss")

        sp = _find_plan_via_selenium(
            driver, wait, cfg, raw_song_query, song_query
        )
        if sp:
            _append_candidates_from_attempts(
                candidates,
                "hitmo:selenium",
                raw_song_query,
                song_query,
                sp,
                seen_urls,
            )
        else:
            diag.append(f"hitmo:selenium:{song_query}=miss")

        lp = _find_lmusic_plan_via_selenium(
            driver, wait, cfg, raw_song_query, song_query
        )
        if lp:
            _append_candidates_from_attempts(
                candidates,
                "lmusic:selenium",
                raw_song_query,
                song_query,
                lp,
                seen_urls,
            )
        else:
            diag.append(f"lmusic:selenium:{song_query}=miss")

        zp = try_zvyki_http_plan(cfg, raw_song_query, song_query, artist, title)
        if zp:
            _append_candidates_from_attempts(
                candidates,
                "zvyki:http",
                raw_song_query,
                song_query,
                zp,
                seen_urls,
            )
        else:
            diag.append(f"zvyki:http:{song_query}=miss")

        if len(candidates) >= 20:
            break

    ranked = quality_layer.rank_candidates(raw_song_query, candidates) if candidates else []
    reason = "; ".join(diag[:8]) if diag else "no_provider_match"
    return ranked, reason, requested_flags, generated_queries


def _candidate_report_entry(
    raw_song_query: str,
    requested_flags: list[str],
    generated_queries: list[str],
    candidates: list[quality_layer.TrackCandidate],
    decision_reason: str | None = None,
    provider_results: list | None = None,
    from_cache: bool = False,
    cache_key: str | None = None,
    cache_hit_type: str | None = None,
    skipped_selenium_reason: str | None = None,
) -> dict:
    candidate_dicts = []
    for candidate in candidates:
        data = quality_layer.candidate_to_dict(candidate) or {}
        extra_reason = getattr(candidate, "decision_reason", None)
        if extra_reason:
            data["decision_reason"] = extra_reason
        candidate_dicts.append(data)
    return {
        "input_track": raw_song_query,
        "requested_flags": requested_flags,
        "generated_queries": generated_queries,
        "decision_reason": decision_reason,
        "from_cache": from_cache,
        "cache_key": cache_key,
        "cache_hit_type": cache_hit_type,
        "skipped_selenium_reason": skipped_selenium_reason,
        "provider_results": [
            {
                "provider": getattr(result, "provider", ""),
                "status": getattr(result, "status", ""),
                "reason": getattr(result, "reason", None),
                "from_cache": getattr(result, "from_cache", False),
                "candidate_count": len(getattr(result, "candidates", []) or []),
            }
            for result in (provider_results or [])
        ],
        "candidates": candidate_dicts,
    }


def _resolve_quality_plan(
    driver,
    wait: WebDriverWait,
    cfg: DownloadConfig,
    raw_song_query: str,
    expected_duration_sec: int | None = None,
) -> dict:
    from search import (
        HitmoHTTPProvider,
        LMusicHTTPProvider,
        SearchOrchestrator,
        SeleniumFallbackProvider,
        TrackQuery,
        ZvykiHTTPProvider,
    )
    from search.cache import SEARCH_LOGIC_VERSION, SearchCache

    artist, title = parse_artist_title(raw_song_query)
    requested_flags = sorted(quality_layer.detect_requested_version_flags(raw_song_query))
    generated_queries = search_query_variants(cfg, raw_song_query)
    enabled_providers = ["hitmo:http"]
    if cfg.lmusic_fallback_enabled:
        enabled_providers.append("lmusic:http")
    if cfg.zvyki_fallback_enabled:
        enabled_providers.append("zvyki:http")
    if cfg.enable_selenium_fallback:
        enabled_providers.append("selenium:fallback")
    track = TrackQuery(
        raw=raw_song_query,
        artist=artist,
        title=title,
        expected_duration_sec=expected_duration_sec,
        generated_queries=generated_queries,
        requested_flags=requested_flags,
        cache_context={
            "search_mode": cfg.search_mode,
            "enabled_providers": enabled_providers,
            "enable_selenium_fallback": cfg.enable_selenium_fallback,
            "provider_origins": {
                "hitmo": cfg.site_origin,
                "lmusic": cfg.lmusic_origin,
                "zvyki": cfg.zvyki_origin,
            },
            "search_logic_version": SEARCH_LOGIC_VERSION,
        },
    )
    cache_path = cfg.search_cache_path or str(Path("downloaded_music") / "search_cache.sqlite3")
    cache = SearchCache(
        cache_path,
        ttl_days=cfg.search_cache_ttl_days,
        negative_ttl_hours=cfg.negative_cache_ttl_hours,
        bypass_negative_cache_for_deep=cfg.bypass_negative_cache_for_deep,
        enabled=cfg.search_cache_enabled,
    )
    selenium_provider = None
    if cfg.enable_selenium_fallback and driver is not None and wait is not None:
        selenium_provider = SeleniumFallbackProvider(cfg, driver, wait)
    orchestrator = SearchOrchestrator(
        [HitmoHTTPProvider(cfg), LMusicHTTPProvider(cfg), ZvykiHTTPProvider(cfg)],
        selenium_provider=selenium_provider,
        cache=cache,
        http_concurrency=cfg.http_search_concurrency,
        enable_selenium_fallback=cfg.enable_selenium_fallback,
    )
    decision = asyncio.run(orchestrator.search(track))
    ranked = decision.candidates
    downloadable = [
        candidate
        for candidate in ranked
        if not candidate.hard_reject
        and candidate.download_url
        and candidate.score >= quality_layer.LOW_CONFIDENCE_THRESHOLD
    ]
    status = decision.status
    reason = decision.reason
    selected = decision.selected_candidate
    return {
        "raw": raw_song_query,
        "download_candidates": downloadable,
        "all_candidates": ranked,
        "selected_candidate": selected or (ranked[0] if ranked else None),
        "requested_flags": requested_flags,
        "generated_queries": generated_queries,
        "status": status,
        "reason": reason,
        "decision_reason": decision.decision_reason,
        "provider_results": decision.provider_results,
        "from_cache": decision.from_cache,
        "cache_key": getattr(decision, "cache_key", None),
        "cache_hit_type": getattr(decision, "cache_hit_type", None),
        "skipped_selenium_reason": getattr(decision, "skipped_selenium_reason", None),
    }


def try_http_search_plan(
    cfg: DownloadConfig,
    raw_song_query: str,
    song_query: str,
    artist: str,
    title: str,
) -> list[tuple[str, str, str]] | None:
    """Парсинг HTML выдачи поиска без Selenium; при успехе — готовый план для http_download_mp3."""
    if not cfg.search_try_http_first:
        return None
    encoded = urllib.parse.quote(song_query, safe="")
    search_url = f"{cfg.site_origin}/search?q={encoded}"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    headers = {
        "User-Agent": cfg.chrome_ua,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }
    req = urllib.request.Request(search_url, headers=headers)
    try:
        with opener.open(req, timeout=cfg.http_search_timeout) as resp:
            raw_bytes = resp.read()
    except Exception as e:
        log.debug("HTTP-поиск %s: %s", _track_preview(song_query), e)
        return None
    html = ""
    for enc in ("utf-8", "utf-8-sig", "cp1251"):
        try:
            html = raw_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not html:
        html = raw_bytes.decode("utf-8", errors="replace")

    urls = _extract_get_music_mp3_urls(html, cfg.site_origin)
    if not urls:
        return None
    urls.sort(
        key=lambda u: score_mp3_url_path(u, raw_song_query, artist, title),
        reverse=True,
    )
    ck = http_cookie_header_for_site(jar, cfg.site_origin)
    attempts = [(normalize_hitmotop_url(u), search_url, ck) for u in urls[:5]]
    log.info(
        "HTTP-поиск: %s mp3 без Selenium (q=%r)",
        len(attempts),
        _track_preview(song_query, 56),
    )
    return attempts


def _find_lmusic_plan_via_selenium(
    driver,
    wait: WebDriverWait,
    cfg: DownloadConfig,
    raw_song_query: str,
    song_query: str,
) -> list[tuple[str, str, str]]:
    if not cfg.lmusic_fallback_enabled:
        return []

    artist, title = parse_artist_title(raw_song_query)
    search_url = cfg.lmusic_origin.rstrip("/")
    track_urls: list[str] = []
    try:
        driver.get(search_url)
        selectors = [
            "input[placeholder*='Какую']",
            "input[type='search']",
            "form input[type='text']",
            "input[type='text']",
        ]
        search_input = None
        for selector in selectors:
            try:
                search_input = wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                )
                if search_input:
                    break
            except TimeoutException:
                continue
        if search_input is None:
            return []

        search_input.clear()
        search_input.send_keys(song_query)
        search_input.send_keys(Keys.ENTER)
        time.sleep(1.2)

        track_urls = collect_lmusic_track_page_urls(driver, cfg.lmusic_origin)
        if not track_urls:
            track_urls = _extract_lmusic_track_urls_from_html(
                driver.page_source, cfg.lmusic_origin
            )
        if not track_urls:
            return []

        track_urls.sort(
            key=lambda u: score_mp3_url_path(u, raw_song_query, artist, title),
            reverse=True,
        )

        attempts: list[tuple[str, str, str]] = []
        for song_page_url in track_urls[:5]:
            try:
                driver.get(song_page_url)
                wait.until(lambda d: "/mp3/" in (d.current_url or "") or "/api/download/" in d.page_source)
            except TimeoutException:
                pass

            download_urls = _extract_lmusic_download_urls(driver.page_source, cfg.lmusic_origin)
            if not download_urls:
                continue

            ck = _cookie_header(driver)
            for download_url in download_urls[:2]:
                attempts.append((download_url, song_page_url, ck))
            if attempts:
                break

        if attempts:
            log.info(
                "LMusic fallback: %s url для %s",
                len(attempts),
                _track_preview(song_query, 56),
            )
        return attempts
    except Exception as exc:
        log.debug("LMusic fallback %s: %s", _track_preview(song_query), exc)
        return []


def load_search_results_page(
    driver, wait: WebDriverWait, search_url: str, cfg: DownloadConfig
) -> None:
    try:
        driver.get(search_url)
    except TimeoutException:
        driver.execute_script("window.stop();")
    rounds = max(1, min(6, cfg.search_load_rounds))
    for attempt in range(rounds):
        try:
            wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "a.track__download-btn")
                )
            )
            return
        except TimeoutException:
            pass
        driver.execute_script(
            "window.scrollTo(0, Math.max(document.body.scrollHeight, 800));"
        )
        time.sleep(
            cfg.search_scroll_sleep_start + attempt * cfg.search_scroll_sleep_step
        )
        tit = driver.title or ""
        if "Результаты" not in tit and attempt < rounds - 1:
            try:
                driver.refresh()
            except TimeoutException:
                driver.execute_script("window.stop();")
            time.sleep(cfg.search_refresh_sleep)
    if not driver.find_elements(By.CSS_SELECTOR, "a.track__download-btn"):
        raise TimeoutException(
            "На странице поиска не появились a.track__download-btn"
        )


def http_download_mp3(
    download_url: str,
    raw_song_query: str,
    referer: str,
    cookie_header: str,
    download_dir: str,
    log_file: str | None,
    site_origin: str,
    chrome_ua: str,
    *,
    quiet: bool = False,
) -> str:
    """Скачивание mp3 по уже известной ссылке (без Selenium)."""
    download_url = normalize_hitmotop_url(download_url)
    base_name = basename_from_mp3_url(download_url)
    filename_safe = sanitize_filename(urllib.parse.unquote(base_name)) + ".mp3"
    file_path = _unique_path(os.path.join(download_dir, filename_safe))
    if not quiet:
        print(f"[GET] {filename_safe}")

    ref = referer or site_origin
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            headers = {
                "User-Agent": chrome_ua,
                "Referer": ref,
                "Accept": "*/*",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            }
            if cookie_header:
                headers["Cookie"] = cookie_header
            req = urllib.request.Request(download_url, headers=headers)
            with urllib.request.urlopen(req, timeout=180) as resp:
                with open(file_path, "wb") as out:
                    total = 0
                    while True:
                        buf = resp.read(64 * 1024)
                        if not buf:
                            break
                        out.write(buf)
                        total += len(buf)
                        if _MAX_DOWNLOAD_BYTES and total > _MAX_DOWNLOAD_BYTES:
                            raise RuntimeError(
                                f"Слишком большой файл ({total} байт), лимит "
                                f"{_MAX_DOWNLOAD_BYTES} (MAX_DOWNLOAD_BYTES)"
                            )
            break
        except Exception as e:
            last_err = e
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except OSError:
                pass
            if attempt < 3:
                time.sleep(0.75 + attempt * 0.75)
    else:
        assert last_err is not None
        raise last_err

    if log_file:
        track_id = base_name.split("_")[-1] if "_" in base_name else base_name
        line = f"{track_id}\t{raw_song_query}\n"
        with _log_write_lock:
            with open(log_file, "a", encoding="utf-8") as log:
                log.write(line)

    if not quiet:
        print(f"[OK] {filename_safe}")
    return file_path


def create_driver(cfg: DownloadConfig):
    ensure_chromedriver_installed()
    options = webdriver.ChromeOptions()
    if cfg.headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1280,900")
    options.add_argument(f"user-agent={cfg.chrome_ua}")
    cd_path = os.environ.get("CHROMEDRIVER_PATH", "").strip()
    if cd_path:
        driver = webdriver.Chrome(service=Service(cd_path), options=options)
    else:
        driver = webdriver.Chrome(options=options)
    timeout = max(5, int(cfg.wait_sec))
    driver.set_page_load_timeout(timeout)
    driver.set_script_timeout(timeout)
    wait = WebDriverWait(driver, cfg.wait_sec)
    return driver, wait


def _find_plan_via_selenium(
    driver,
    wait: WebDriverWait,
    cfg: DownloadConfig,
    raw_song_query: str,
    song_query: str,
) -> list[tuple[str, str, str]]:
    """
    Selenium: выдача поиска и до 5 mp3 с неё, при необходимости — страницы треков.
    """
    attempts: list[tuple[str, str, str]] = []
    artist, title = parse_artist_title(raw_song_query)
    encoded_song = urllib.parse.quote(song_query, safe="")
    search_url = f"{cfg.site_origin}/search?q={encoded_song}"

    try:
        load_search_results_page(driver, wait, search_url, cfg)
        candidates = collect_mp3_candidates_from_search(driver)
        candidates.sort(
            key=lambda pair: score_mp3_candidate(
                pair[0], pair[1], raw_song_query, artist, title
            ),
            reverse=True,
        )
        ck_search = _cookie_header(driver)
        for download_url, _ in candidates[:5]:
            attempts.append(
                (normalize_hitmotop_url(download_url), search_url, ck_search)
            )

        need_track_pages = (not attempts) or cfg.fetch_track_pages_when_search_has_mp3
        if not need_track_pages:
            return attempts

        for song_page_url in collect_track_page_urls(driver, cfg.site_origin):
            try:
                driver.get(song_page_url)
                btn = wait.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "a.track__download-btn")
                    )
                )
                download_url = btn.get_attribute("href")
            except TimeoutException:
                continue
            if not download_url or "/get/music/" not in download_url:
                continue
            ck = _cookie_header(driver)
            attempts.append(
                (normalize_hitmotop_url(download_url), song_page_url, ck)
            )
    except TimeoutException:
        return attempts
    except Exception:
        return attempts
    return attempts


def find_download_plan_for_line(
    driver,
    wait: WebDriverWait,
    cfg: DownloadConfig,
    raw_song_query: str,
) -> list[tuple[str, str, str]]:
    """
    План (url, referer, cookie): сначала HTTP-парсинг выдачи (если включено), иначе Selenium.
    Варианты строки поиска — см. search_transliterate (транслит).
    """
    artist, title = parse_artist_title(raw_song_query)
    for song_query in search_query_variants(cfg, raw_song_query):
        if cfg.search_try_http_first:
            hp = try_http_search_plan(
                cfg, raw_song_query, song_query, artist, title
            )
            if hp:
                return hp
        sp = _find_plan_via_selenium(
            driver, wait, cfg, raw_song_query, song_query
        )
        if sp:
            return sp
        lp = _find_lmusic_plan_via_selenium(
            driver, wait, cfg, raw_song_query, song_query
        )
        if lp:
            return lp
        zp = try_zvyki_http_plan(cfg, raw_song_query, song_query, artist, title)
        if zp:
            return zp
    return []


def diagnose_download_plan_for_line(
    driver,
    wait: WebDriverWait,
    cfg: DownloadConfig,
    raw_song_query: str,
) -> tuple[list[tuple[str, str, str]], str]:
    artist, title = parse_artist_title(raw_song_query)
    diag: list[str] = []
    for song_query in search_query_variants(cfg, raw_song_query):
        if cfg.search_try_http_first:
            hp = try_http_search_plan(
                cfg, raw_song_query, song_query, artist, title
            )
            if hp:
                return hp, "hitmo:http"
            diag.append(f"hitmo:http:{song_query}=miss")
        sp = _find_plan_via_selenium(
            driver, wait, cfg, raw_song_query, song_query
        )
        if sp:
            return sp, "hitmo:selenium"
        diag.append(f"hitmo:selenium:{song_query}=miss")
        lp = _find_lmusic_plan_via_selenium(
            driver, wait, cfg, raw_song_query, song_query
        )
        if lp:
            return lp, "lmusic:selenium"
        diag.append(f"lmusic:selenium:{song_query}=miss")
        zp = try_zvyki_http_plan(cfg, raw_song_query, song_query, artist, title)
        if zp:
            return zp, "zvyki:http"
        diag.append(f"zvyki:http:{song_query}=miss")
    return [], "; ".join(diag[:6]) or "no_provider_match"


def _download_with_attempts(
    raw_song_query: str,
    attempts: list[tuple[str, str, str]],
    download_dir: str,
    log_file: str | None,
    site_origin: str,
    chrome_ua: str,
    *,
    quiet: bool = False,
) -> tuple[str | None, str | None, str | None]:
    last_err: Exception | None = None
    last_url: str | None = None
    for download_url, referer, cookie_header in attempts:
        for candidate_url in candidate_download_urls(download_url, site_origin):
            last_url = candidate_url
            try:
                path = http_download_mp3(
                    last_url,
                    raw_song_query,
                    referer,
                    cookie_header,
                    download_dir,
                    log_file,
                    site_origin,
                    chrome_ua,
                    quiet=quiet,
                )
                return path, None, last_url
            except Exception as exc:
                last_err = exc
                continue
    if last_err is None:
        return None, "нет попыток скачивания", last_url
    return None, f"{type(last_err).__name__}: {last_err}", last_url


def download_one_line(
    driver,
    wait: WebDriverWait,
    cfg: DownloadConfig,
    raw_song_query: str,
    download_dir: str,
    log_file: str | None,
) -> str | None:
    """Один трек: поиск в браузере + скачивание в том же потоке (последовательные HTTP-попытки)."""
    plan = find_download_plan_for_line(driver, wait, cfg, raw_song_query)
    if not plan:
        return None
    path, _error, _last_url = _download_with_attempts(
        raw_song_query,
        plan,
        download_dir,
        log_file,
        cfg.site_origin,
        cfg.chrome_ua,
        quiet=False,
    )
    return path


ProgressCb = Callable[[int, int, str, str], None]


def _phase1_resolve_worker(
    worker_id: int,
    cfg: DownloadConfig,
    task_queue: Queue,
    agg: dict[int, dict],
    lock: threading.Lock,
    total_lines: int,
    on_progress: ProgressCb | None = None,
) -> None:
    driver = None
    wait = None
    if cfg.enable_selenium_fallback:
        driver, wait = create_driver(cfg)
    try:
        while True:
            item = task_queue.get()
            if item is _STOP_WORKER:
                break
            i, raw, expected_duration_sec = item
            worker_kind = "Chrome" if cfg.enable_selenium_fallback else "HTTP"
            log.info(
                "Фаза 1 #%s/%s [%s %s]: поиск… %s",
                i,
                total_lines,
                worker_kind,
                worker_id,
                _track_preview(raw),
            )
            t_resolve = time.monotonic()
            try:
                try:
                    resolved_info = _resolve_quality_plan(driver, wait, cfg, raw, expected_duration_sec)
                except Exception as exc:
                    log.exception("quality resolve failed for %s", _track_preview(raw))
                    resolved_info = {
                        "raw": raw,
                        "download_candidates": [],
                        "all_candidates": [],
                        "selected_candidate": None,
                        "requested_flags": sorted(
                            quality_layer.detect_requested_version_flags(raw)
                        ),
                        "generated_queries": search_query_variants(cfg, raw),
                        "status": quality_layer.STATUS_NEEDS_MANUAL_REVIEW,
                        "reason": f"quality_exception: {type(exc).__name__}: {exc}",
                    }
            except Exception as exc:
                log.warning(
                    "Фаза 1 #%s/%s [%s %s]: сбой — %s: %s",
                    i,
                    total_lines,
                    worker_kind,
                    worker_id,
                    _track_preview(raw),
                    exc,
                )
                resolved_info = {
                    "raw": raw,
                    "download_candidates": [],
                    "all_candidates": [],
                    "selected_candidate": None,
                    "requested_flags": sorted(
                        quality_layer.detect_requested_version_flags(raw)
                    ),
                    "generated_queries": search_query_variants(cfg, raw),
                    "status": quality_layer.STATUS_NEEDS_MANUAL_REVIEW,
                    "reason": f"exception: {type(exc).__name__}: {exc}",
                }
            if resolved_info.get("download_candidates"):
                log.info(
                    "Фаза 1 #%s/%s [%s %s]: план из %s URL — %s",
                    i,
                    total_lines,
                    worker_kind,
                    worker_id,
                    len(resolved_info.get("download_candidates") or []),
                    _track_preview(raw),
                )
            else:
                log.warning(
                    "Фаза 1 #%s/%s [%s %s]: ссылок не найдено — %s",
                    i,
                    total_lines,
                    worker_kind,
                    worker_id,
                    _track_preview(raw),
                )
            with lock:
                agg[i] = resolved_info
                done_count = len(agg)
            if on_progress:
                on_progress(done_count, total_lines, raw, "resolve")
            _sleep_after_phase1_track(cfg, time.monotonic() - t_resolve)
    finally:
        if driver is not None:
            driver.quit()


def _run_root_for_download_dir(download_dir: str) -> str:
    p = Path(download_dir)
    return str(p.parent if p.name.lower() == "music" else p)


def _write_success_log(log_file: str | None, raw_song_query: str, download_url: str | None) -> None:
    if not log_file:
        return
    base_name = basename_from_mp3_url(download_url or "")
    track_id = base_name.split("_")[-1] if "_" in base_name else base_name
    with _log_write_lock:
        with open(log_file, "a", encoding="utf-8") as log_f:
            log_f.write(f"{track_id}\t{raw_song_query}\n")


def _download_quality_candidate(
    raw_song_query: str,
    candidate: quality_layer.TrackCandidate,
    download_dir: str,
    cfg: DownloadConfig,
    quiet: bool,
) -> tuple[str | None, str | None, str | None]:
    last_err: Exception | None = None
    last_url: str | None = None
    if not candidate.download_url:
        return None, "candidate_without_download_url", None
    for candidate_url in candidate_download_urls(candidate.download_url, cfg.site_origin):
        last_url = candidate_url
        try:
            path = http_download_mp3(
                candidate_url,
                raw_song_query,
                candidate.referer or candidate.source_url or cfg.site_origin,
                candidate.cookie_header,
                download_dir,
                None,
                cfg.site_origin,
                cfg.chrome_ua,
                quiet=quiet,
            )
            return path, None, last_url
        except Exception as exc:
            last_err = exc
            continue
    if last_err is None:
        return None, "no_download_attempts", last_url
    return None, f"{type(last_err).__name__}: {last_err}", last_url


def _run_resolved_download(
    rt: ResolvedTrack,
    download_dir: str,
    log_file: str | None,
    cfg: DownloadConfig,
    quiet: bool,
    validation_sem: threading.Semaphore | None = None,
) -> tuple[int, str, str | None, str, str | None, str | None, dict]:
    run_root = _run_root_for_download_dir(download_dir)
    accepted_dir = os.path.join(run_root, "accepted")
    rejected_dir = os.path.join(run_root, "rejected")
    os.makedirs(download_dir, exist_ok=True)
    os.makedirs(accepted_dir, exist_ok=True)
    os.makedirs(rejected_dir, exist_ok=True)

    last_error: str | None = None
    last_url: str | None = None
    last_item: dict | None = None
    candidates = rt.candidates or [
        _candidate_from_attempt("legacy", rt.raw_song_query, rt.raw_song_query, attempt)
        for attempt in rt.attempts
    ]

    for candidate in candidates:
        path, error, used_url = _download_quality_candidate(
            rt.raw_song_query,
            candidate,
            download_dir,
            cfg,
            quiet=quiet,
        )
        last_error = error
        last_url = used_url
        if not path:
            last_item = quality_layer.make_quality_item(
                rt.raw_song_query,
                quality_layer.STATUS_DOWNLOAD_FAILED,
                reason=error,
                selected_candidate=candidate,
                url=used_url,
            )
            continue

        if validation_sem is not None:
            validation_sem.acquire()
        try:
            validation = quality_layer.validate_downloaded_file(
                path,
                rt.raw_song_query,
                candidate,
            )
        finally:
            if validation_sem is not None:
                validation_sem.release()
        candidate.file_size = validation.file_size
        candidate.bitrate = validation.bitrate
        candidate.duration_sec = validation.duration_sec or candidate.duration_sec

        if validation.ok and candidate.score >= quality_layer.POST_DOWNLOAD_OK_SCORE_THRESHOLD:
            accepted_path = quality_layer.move_file_to_dir(path, accepted_dir)
            _write_success_log(log_file, rt.raw_song_query, used_url)
            item = quality_layer.make_quality_item(
                rt.raw_song_query,
                quality_layer.STATUS_OK,
                selected_candidate=candidate,
                validation=validation,
                file=accepted_path,
                url=used_url,
            )
            return (
                rt.playlist_index,
                rt.raw_song_query,
                accepted_path,
                quality_layer.STATUS_OK,
                None,
                used_url,
                item,
            )

        status = validation.status if not validation.ok else quality_layer.STATUS_LOW_CONFIDENCE_MATCH
        reason = validation.reason if not validation.ok else "post_download_score_below_ok_threshold"
        rejected_path = quality_layer.move_file_to_dir(path, rejected_dir)
        last_item = quality_layer.make_quality_item(
            rt.raw_song_query,
            status,
            reason=reason,
            selected_candidate=candidate,
            validation=validation,
            file=rejected_path,
            url=used_url,
        )
        last_error = reason

    if last_item is None:
        last_item = quality_layer.make_quality_item(
            rt.raw_song_query,
            quality_layer.STATUS_DOWNLOAD_FAILED,
            reason=last_error or "no_candidate_downloaded",
            selected_candidate=(candidates[0] if candidates else None),
            url=last_url,
        )
    return (
        rt.playlist_index,
        rt.raw_song_query,
        None,
        str(last_item.get("status") or quality_layer.STATUS_NEEDS_MANUAL_REVIEW),
        last_error,
        last_url,
        last_item,
    )


def run_playlist_download(
    lines: list[str],
    download_dir: str,
    *,
    log_file: str | None = None,
    skip_lines: set[str] | None = None,
    on_progress: ProgressCb | None = None,
    cfg: DownloadConfig | None = None,
) -> dict:
    """
    Фаза 1: поиск ссылок в Chrome (один или несколько браузеров — resolve_parallel_workers).
    Фаза 2: до parallel_download_workers параллельных HTTP-скачиваний без Selenium.

    Возвращает
    { "ok": [(line, path), ...], "failed": [line, ...], "skipped": [line, ...] }
    """
    cfg = cfg or DownloadConfig()
    pw = cfg.download_concurrency or cfg.parallel_download_workers
    if pw <= 0:
        pw = 10
    pw = max(1, min(32, pw))
    rw = cfg.resolve_parallel_workers
    if rw <= 0:
        rw = 1
    if cfg.enable_selenium_fallback:
        rw = max(1, min(max(1, cfg.selenium_max_workers), rw))
    else:
        rw = max(1, min(16, cfg.http_search_concurrency, rw))

    os.makedirs(download_dir, exist_ok=True)
    run_root = _run_root_for_download_dir(download_dir)
    accepted_dir = os.path.join(run_root, "accepted")
    rejected_dir = os.path.join(run_root, "rejected")
    os.makedirs(accepted_dir, exist_ok=True)
    os.makedirs(rejected_dir, exist_ok=True)
    if log_file:
        log_parent = os.path.dirname(log_file)
        if log_parent:
            os.makedirs(log_parent, exist_ok=True)
        with open(log_file, "a", encoding="utf-8"):
            pass
    skip = skip_lines or set()
    failed: list[str] = []
    failed_details: dict[str, str] = {}
    download_failed_details: dict[str, str] = {}
    skipped: list[str] = []
    quality_items: list[dict] = []
    candidates_report: list[dict] = []
    clean_items = [
        (normalized, extract_playlist_duration_sec(ln))
        for ln in lines
        if (normalized := normalize_playlist_line(ln))
    ]
    clean_lines = [item[0] for item in clean_items]
    total = len(clean_lines)
    log.info(
        "Старт: строк=%s, пауза между треками=%ss, фаза1 Chrome=%s, фаза2 HTTP=%s",
        total,
        cfg.delay_sec,
        rw,
        pw,
    )
    if on_progress:
        on_progress(0, total, "", "phase1_start")

    resolved: list[ResolvedTrack] = []
    resolve_miss = 0

    task_items: list[tuple[int, str, int | None]] = []
    for i, (raw, expected_duration_sec) in enumerate(clean_items, 1):
        if raw in skip:
            skipped.append(raw)
            log.info("Фаза 1 %s/%s: пропуск (уже в логе) — %s", i, total, _track_preview(raw))
            if on_progress:
                on_progress(i, total, raw, "skip")
            time.sleep(min(cfg.delay_sec, 0.15))
            continue
        task_items.append((i, raw, expected_duration_sec))

    if not task_items:
        log.info("Фаза 1: нечего искать (все строки в пропуске)")
    elif rw <= 1:
        driver = None
        wait = None
        if cfg.enable_selenium_fallback:
            driver, wait = create_driver(cfg)
        try:
            if cfg.enable_selenium_fallback:
                log.info("Фаза 1: HTTP providers + Selenium fallback, потоков=%s", rw)
            else:
                log.info("Фаза 1: HTTP providers без Selenium, потоков=%s", rw)
            for i, raw, expected_duration_sec in task_items:
                if on_progress:
                    on_progress(i, total, raw, "resolve")
                log.info("Фаза 1 %s/%s: поиск… %s", i, total, _track_preview(raw))
                t_resolve = time.monotonic()
                resolved_info = _resolve_quality_plan(driver, wait, cfg, raw, expected_duration_sec)
                candidates_report.append(
                    _candidate_report_entry(
                        raw,
                        resolved_info["requested_flags"],
                        resolved_info["generated_queries"],
                        resolved_info["all_candidates"],
                        resolved_info.get("decision_reason"),
                        resolved_info.get("provider_results"),
                        bool(resolved_info.get("from_cache")),
                        resolved_info.get("cache_key"),
                        resolved_info.get("cache_hit_type"),
                        resolved_info.get("skipped_selenium_reason"),
                    )
                )
                if not resolved_info["download_candidates"]:
                    resolve_miss += 1
                    failed.append(raw)
                    status = resolved_info.get("status") or quality_layer.STATUS_NEEDS_MANUAL_REVIEW
                    reason = resolved_info.get("reason") or status
                    failed_details[raw] = reason
                    quality_items.append(
                        quality_layer.make_quality_item(
                            raw,
                            status,
                            reason=reason,
                            selected_candidate=resolved_info.get("selected_candidate"),
                        )
                    )
                    log.warning("Фаза 1 %s/%s: ссылок не найдено — %s", i, total, _track_preview(raw))
                    if on_progress:
                        on_progress(i, total, raw, status)
                else:
                    resolved.append(
                        ResolvedTrack(
                            playlist_index=i,
                            raw_song_query=raw,
                            attempts=[
                                (
                                    c.download_url or "",
                                    c.referer or c.source_url or cfg.site_origin,
                                    c.cookie_header,
                                )
                                for c in resolved_info["download_candidates"]
                            ],
                            candidates=resolved_info["download_candidates"],
                            requested_flags=resolved_info["requested_flags"],
                            generated_queries=resolved_info["generated_queries"],
                            expected_duration_sec=expected_duration_sec,
                        )
                    )
                    log.info(
                        "Фаза 1 %s/%s: план из %s URL — %s",
                        i,
                        total,
                        len(resolved_info["download_candidates"]),
                        _track_preview(raw),
                    )
                _sleep_after_phase1_track(cfg, time.monotonic() - t_resolve)
        finally:
            if driver is not None:
                driver.quit()
    else:
        if cfg.enable_selenium_fallback:
            log.info("Фаза 1: HTTP providers + Selenium fallback, потоков=%s", rw)
        else:
            log.info("Фаза 1: HTTP providers без Selenium, потоков=%s", rw)
        q: Queue = Queue()
        for job in task_items:
            q.put(job)
        for _ in range(rw):
            q.put(_STOP_WORKER)
        agg: dict[int, dict] = {}
        lock = threading.Lock()
        threads: list[threading.Thread] = []
        for w in range(rw):
            t = threading.Thread(
                target=_phase1_resolve_worker,
                args=(w + 1, cfg, q, agg, lock, total),
                kwargs={"on_progress": on_progress},
                name=f"hitmo-resolve-{w + 1}",
                daemon=False,
            )
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        for i, raw, expected_duration_sec in task_items:
            if i not in agg:
                log.error(
                    "Фаза 1 #%s/%s: задача не обработана (сбой воркера?) — %s",
                    i,
                    total,
                    _track_preview(raw),
                )
                resolve_miss += 1
                failed.append(raw)
                status = quality_layer.STATUS_NEEDS_MANUAL_REVIEW
                reason = "resolve_worker_missing_result"
                failed_details[raw] = reason
                quality_items.append(
                    quality_layer.make_quality_item(raw, status, reason=reason)
                )
                if on_progress:
                    on_progress(i, total, raw, status)
                continue
            resolved_info = agg[i]
            candidates_report.append(
                _candidate_report_entry(
                    raw,
                    resolved_info["requested_flags"],
                    resolved_info["generated_queries"],
                    resolved_info["all_candidates"],
                    resolved_info.get("decision_reason"),
                    resolved_info.get("provider_results"),
                    bool(resolved_info.get("from_cache")),
                    resolved_info.get("cache_key"),
                    resolved_info.get("cache_hit_type"),
                    resolved_info.get("skipped_selenium_reason"),
                )
            )
            if not resolved_info["download_candidates"]:
                resolve_miss += 1
                failed.append(raw)
                status = resolved_info.get("status") or quality_layer.STATUS_NEEDS_MANUAL_REVIEW
                reason = resolved_info.get("reason") or status
                if reason:
                    failed_details[raw] = reason
                quality_items.append(
                    quality_layer.make_quality_item(
                        raw,
                        status,
                        reason=reason,
                        selected_candidate=resolved_info.get("selected_candidate"),
                    )
                )
                if on_progress:
                    on_progress(i, total, raw, status)
            else:
                resolved.append(
                    ResolvedTrack(
                        playlist_index=i,
                        raw_song_query=raw,
                        attempts=[
                            (
                                c.download_url or "",
                                c.referer or c.source_url or cfg.site_origin,
                                c.cookie_header,
                            )
                            for c in resolved_info["download_candidates"]
                        ],
                        candidates=resolved_info["download_candidates"],
                        requested_flags=resolved_info["requested_flags"],
                        generated_queries=resolved_info["generated_queries"],
                        expected_duration_sec=expected_duration_sec,
                    )
                )

    log.info(
        "Фаза 1 окончена: с планом=%s, без ссылок=%s, пропуск=%s",
        len(resolved),
        resolve_miss,
        len(skipped),
    )

    ok_items: list[tuple[int, str, str]] = []
    quiet_dl = pw > 1
    validation_sem = threading.Semaphore(max(1, min(32, cfg.validation_parallel_workers)))

    if resolved:
        if on_progress:
            on_progress(0, total, "", "phase2_start")
        log.info("Фаза 2 (HTTP): скачиваю %s файлов, потоков=%s", len(resolved), pw)
        if pw == 1:
            n_done = 0
            for rt in sorted(resolved, key=lambda r: r.playlist_index):
                _i, raw, path, final_status, error, last_url, quality_item = _run_resolved_download(
                    rt, download_dir, log_file, cfg, quiet=False, validation_sem=validation_sem
                )
                n_done += 1
                quality_items.append(quality_item)
                if path:
                    ok_items.append((_i, raw, path))
                    log.info(
                        "Фаза 2 %s/%s: OK — %s",
                        n_done,
                        len(resolved),
                        _track_preview(raw),
                    )
                    if on_progress:
                        on_progress(_i, total, raw, "ok")
                else:
                    failed.append(raw)
                    last_host = (
                        urllib.parse.urlparse(last_url).netloc if last_url else "-"
                    )
                    download_failed_details[raw] = (
                        f"host={last_host}; url={last_url or '-'}; error={error or 'неизвестно'}"
                    )
                    log.warning(
                        "Фаза 2 %s/%s: не скачалось — %s; host: %s; url: %s; причина: %s",
                        n_done,
                        len(resolved),
                        _track_preview(raw),
                        last_host,
                        last_url or "-",
                        error or "неизвестно",
                    )
                    if on_progress:
                        on_progress(_i, total, raw, final_status)
        else:
            n_done = 0
            with ThreadPoolExecutor(max_workers=pw) as pool:
                future_map = {
                    pool.submit(
                        _run_resolved_download,
                        rt,
                        download_dir,
                        log_file,
                        cfg,
                        quiet_dl,
                        validation_sem,
                    ): rt
                    for rt in resolved
                }
                for fut in as_completed(future_map):
                    rt = future_map[fut]
                    n_done += 1
                    try:
                        idx, raw, path, final_status, error, last_url, quality_item = fut.result()
                        quality_items.append(quality_item)
                    except Exception as exc:
                        final_status = quality_layer.STATUS_DOWNLOAD_FAILED
                        idx, raw, path, error, last_url = (
                            rt.playlist_index,
                            rt.raw_song_query,
                            None,
                            f"{type(exc).__name__}: {exc}",
                            None,
                        )
                        quality_item = quality_layer.make_quality_item(
                            raw,
                            final_status,
                            reason=error,
                            selected_candidate=(rt.candidates[0] if rt.candidates else None),
                        )
                        quality_items.append(quality_item)
                        log.exception(
                            "Фаза 2 %s/%s: исключение — %s: %s",
                            n_done,
                            len(resolved),
                            _track_preview(rt.raw_song_query),
                            exc,
                        )
                    if path:
                        ok_items.append((idx, raw, path))
                        log.info(
                            "Фаза 2 %s/%s: OK — %s",
                            n_done,
                            len(resolved),
                            _track_preview(raw),
                        )
                        if on_progress:
                            on_progress(idx, total, raw, "ok")
                    else:
                        failed.append(raw)
                        last_host = (
                            urllib.parse.urlparse(last_url).netloc if last_url else "-"
                        )
                        download_failed_details[raw] = (
                            f"host={last_host}; url={last_url or '-'}; error={error or 'неизвестно'}"
                        )
                        log.warning(
                            "Фаза 2 %s/%s: не скачалось — %s; host: %s; url: %s; причина: %s",
                            n_done,
                            len(resolved),
                            _track_preview(raw),
                            last_host,
                            last_url or "-",
                            error or "неизвестно",
                        )
                        if on_progress:
                            on_progress(idx, total, raw, final_status)
        log.info("Фаза 2 окончена")
    else:
        log.info("Фаза 2 пропущена: нечего качать (нет планов ссылок)")

    ok_items.sort(key=lambda t: t[0])
    ok: list[tuple[str, str]] = [(raw, path) for _, raw, path in ok_items]
    order = {raw: i for i, raw in enumerate(clean_lines)}
    quality_items.sort(key=lambda item: order.get(str(item.get("input_track")), total + 1))
    candidates_report.sort(key=lambda item: order.get(str(item.get("input_track")), total + 1))
    try:
        report_files = quality_layer.write_quality_reports(
            run_root,
            quality_items,
            candidates_report,
            total=total,
        )
    except Exception as exc:
        log.exception("quality report generation failed: %s", exc)
        report_files = {}
    quality_counts = quality_layer.quality_counts_for_api(quality_items, total=total)
    log.info(
        "Итог: скачано=%s, ошибок (нет ссылок или HTTP)=%s, пропуск=%s",
        len(ok),
        len(failed),
        len(skipped),
    )
    return {
        "ok": ok,
        "failed": failed,
        "failed_details": failed_details,
        "download_failed_details": download_failed_details,
        "skipped": skipped,
        "quality_items": quality_items,
        "quality_counts": quality_counts,
        "report_files": report_files,
        "accepted_dir": accepted_dir,
        "rejected_dir": rejected_dir,
        "candidates": candidates_report,
    }
