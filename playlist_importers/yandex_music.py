from __future__ import annotations

import csv
import json
import logging
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, unquote, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import (
    ImportedPlaylist,
    ImportedTrack,
    PlaylistImporter,
    PLAIN_TEXT,
    UNKNOWN,
    YANDEX_IFRAME,
    YANDEX_PLAYLIST_URL,
    YANDEX_SAVED_HTML,
)
from .html_utils import format_duration_ms, parse_duration_to_ms, read_text_file, soup_from_html
from .text_importer import TextPlaylistImporter

log = logging.getLogger("playlist_import")

YANDEX_SERVICE = "yandex_music"
IMPORT_ROOT = Path("downloaded_music") / "imports"

WARNING_IFRAME_NO_TRACKS = (
    "Iframe распознан, но список треков в iframe-коде отсутствует. "
    "Загрузите сохраненный HTML плейлиста для полного импорта."
)
WARNING_URL_ONLY = (
    "Ссылка распознана, но сетевой импорт на первом этапе не выполняется. "
    "Загрузите сохраненный HTML страницы плейлиста для извлечения треков."
)
WARNING_DOM_PARTIAL = (
    "DOM fallback может вернуть неполный список треков. Для полного импорта "
    "сохраните страницу после прокрутки списка до конца или используйте HTML с __NEXT_DATA__."
)
FETCH_TIMEOUT_SEC = 15
YANDEX_FETCH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
YANDEX_API_BASE = "https://api.music.yandex.ru"
YANDEX_API_PAGE_SIZE = 100
YANDEX_API_MAX_PAGES = 20
YANDEX_API_RETRIES_PER_PAGE = 3

WARNING_URL_ONLY = (
    "Ссылка распознана. Система пытается загрузить публичную страницу плейлиста, "
    "но Яндекс Музыка может вернуть страницу входа или неполные данные. "
    "Если треки не появились, загрузите сохраненный HTML страницы плейлиста."
)


_URL_RE = re.compile(r"https?://music\.yandex\.(?:ru|com|kz|by|uz)[^\s\"'<>]+", re.I)
_IFRAME_RE = re.compile(r"music\.yandex\.[^/]+/iframe/playlist/([^/?#]+)/([^/?#]+)", re.I)
_USER_PLAYLIST_RE = re.compile(r"music\.yandex\.[^/]+/users/([^/?#]+)/playlists/([^/?#]+)", re.I)
_UUID_PLAYLIST_RE = re.compile(r"music\.yandex\.[^/]+/playlists/([^/?#]+)", re.I)


def _first_yandex_url(text: str) -> str | None:
    match = _URL_RE.search(text or "")
    return match.group(0).rstrip(").,;") if match else None


def _owner_kind_from_url(url: str) -> tuple[str | None, str | None, str | None]:
    for regex in (_IFRAME_RE, _USER_PLAYLIST_RE):
        match = regex.search(url or "")
        if match:
            return unquote(match.group(1)), unquote(match.group(2)), None
    match = _UUID_PLAYLIST_RE.search(url or "")
    if match:
        return None, unquote(match.group(1)), "YANDEX_URL_UNSUPPORTED_FORMAT"
    return None, None, "YANDEX_URL_UNSUPPORTED_FORMAT"


def _public_playlist_url(owner: str | None, kind: str | None, fallback_url: str | None = None) -> str | None:
    if fallback_url:
        return fallback_url
    if owner and kind:
        return f"https://music.yandex.ru/users/{owner}/playlists/{kind}"
    return fallback_url


def _playlist_api_url(owner: str | None, kind: str | None, fallback_url: str | None = None) -> str | None:
    if owner and kind:
        return f"{YANDEX_API_BASE}/users/{owner}/playlists/{kind}"
    if kind and (not owner) and fallback_url and _UUID_PLAYLIST_RE.search(fallback_url or ""):
        return f"{YANDEX_API_BASE}/playlist/{kind}"
    return None


def _fetch_public_json(url: str) -> tuple[dict[str, Any] | None, str | None]:
    if not url:
        return None, "YANDEX_FETCH_FAILED: отсутствует URL публичного API плейлиста."
    curl_bin = shutil.which("curl.exe") or shutil.which("curl")
    if curl_bin:
        try:
            proc = subprocess.run(
                [
                    curl_bin,
                    "-sS",
                    "-L",
                    "-H",
                    f"User-Agent: {YANDEX_FETCH_USER_AGENT}",
                    "-H",
                    "Accept: application/json,text/plain,*/*",
                    "-H",
                    "Accept-Language: ru,en;q=0.9",
                    url,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=FETCH_TIMEOUT_SEC,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                try:
                    return json.loads(proc.stdout), None
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass

    req = Request(
        url,
        headers={
            "User-Agent": YANDEX_FETCH_USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "ru,en;q=0.9",
        },
    )
    try:
        with urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read()
        try:
            text = raw.decode(charset, errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
        return json.loads(text), None
    except json.JSONDecodeError:
        return None, "YANDEX_FETCH_FAILED: публичный API плейлиста вернул невалидный JSON."
    except HTTPError as exc:
        return None, f"YANDEX_FETCH_FAILED: публичный API плейлиста недоступен ({exc.code})."
    except URLError as exc:
        return None, f"YANDEX_FETCH_FAILED: не удалось обратиться к публичному API плейлиста ({exc.reason})."
    except Exception as exc:
        return None, f"YANDEX_FETCH_FAILED: не удалось обратиться к публичному API плейлиста ({exc})."


def _playlist_from_public_api(
    owner: str | None,
    kind: str | None,
    fallback_url: str | None = None,
) -> ImportedPlaylist | None:
    api_base_url = _playlist_api_url(owner, kind, fallback_url)
    if not api_base_url:
        return None

    all_tracks: list[dict[str, Any]] = []
    seen_track_ids: set[str] = set()
    playlist_result: dict[str, Any] | None = None
    warnings: list[str] = []
    expected_pages: int | None = None

    for page in range(YANDEX_API_MAX_PAGES):
        query = urlencode(
            {
                "richTracks": "true",
                "page": page,
                "pageSize": YANDEX_API_PAGE_SIZE,
            }
        )
        payload = None
        warning = None
        for _attempt in range(YANDEX_API_RETRIES_PER_PAGE):
            payload, warning = _fetch_public_json(f"{api_base_url}?{query}")
            if payload is not None:
                break
        if payload is None:
            if page == 0:
                return ImportedPlaylist(
                    title=None,
                    source_service=YANDEX_SERVICE,
                    source_url=fallback_url,
                    tracks=[],
                    owner=owner,
                    kind=kind,
                    warnings=[warning or WARNING_URL_ONLY],
                    raw_data={"method_used": "public_api", "api_url": api_base_url},
                )
            warnings.append(warning or "YANDEX_FETCH_FAILED: не удалось получить следующую страницу плейлиста.")
            break

        result = payload.get("result")
        if not isinstance(result, dict):
            return ImportedPlaylist(
                title=None,
                source_service=YANDEX_SERVICE,
                source_url=fallback_url,
                tracks=[],
                owner=owner,
                kind=kind,
                warnings=["YANDEX_FETCH_FAILED: публичный API плейлиста вернул неожиданный ответ."],
                raw_data={"method_used": "public_api", "api_url": api_base_url, "payload": payload},
            )

        if playlist_result is None:
            playlist_result = result

        page_tracks = result.get("tracks") or []
        if not isinstance(page_tracks, list) or not page_tracks:
            break
        for track in page_tracks:
            if not isinstance(track, dict):
                continue
            raw_track = track.get("track") if isinstance(track.get("track"), dict) else track
            track_id = raw_track.get("id") or raw_track.get("realId") or track.get("id")
            track_key = str(track_id) if track_id is not None else json.dumps(track, ensure_ascii=False, sort_keys=True)
            if track_key in seen_track_ids:
                continue
            seen_track_ids.add(track_key)
            all_tracks.append(track)

        pager = result.get("pager") if isinstance(result.get("pager"), dict) else {}
        total = pager.get("total")
        per_page = pager.get("perPage") or YANDEX_API_PAGE_SIZE
        try:
            total_int = int(total) if total is not None else None
        except (TypeError, ValueError):
            total_int = None
        try:
            per_page_int = max(1, int(per_page))
        except (TypeError, ValueError):
            per_page_int = YANDEX_API_PAGE_SIZE
        if total_int is not None and expected_pages is None:
            expected_pages = max(1, (total_int + per_page_int - 1) // per_page_int)
        if total_int is not None and len(all_tracks) >= total_int:
            break
        if expected_pages is not None and page + 1 >= expected_pages:
            break

    if playlist_result is None:
        return None

    payload = dict(playlist_result)
    payload["tracks"] = all_tracks
    playlist = _playlist_from_payload(payload, fallback_url, {"method_used": "public_api", "payload": payload})
    owner_info = playlist_result.get("owner") if isinstance(playlist_result.get("owner"), dict) else {}
    playlist.owner = playlist.owner or owner or owner_info.get("login")
    api_kind = playlist_result.get("kind")
    playlist.kind = playlist.kind or kind or (str(api_kind) if api_kind is not None else None)
    playlist.profile_name = owner_info.get("name") if isinstance(owner_info, dict) else None
    if playlist.owner:
        playlist.profile_url = f"https://music.yandex.ru/users/{playlist.owner}"
    playlist.warnings.extend(warnings)
    playlist.raw_data = {"method_used": "public_api", "api_url": api_base_url, "payload": payload}
    return playlist


def _fetch_public_html(url: str) -> tuple[str | None, str | None]:
    if not url:
        return None, "YANDEX_URL_UNSUPPORTED_FORMAT: отсутствует URL плейлиста."
    req = Request(
        url,
        headers={
            "User-Agent": YANDEX_FETCH_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru,en;q=0.9",
        },
    )
    try:
        with urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read()
        try:
            return raw.decode(charset, errors="replace"), None
        except LookupError:
            return raw.decode("utf-8", errors="replace"), None
    except HTTPError as exc:
        return None, f"YANDEX_FETCH_FAILED: страница плейлиста недоступна ({exc.code})."
    except URLError as exc:
        return None, f"YANDEX_FETCH_FAILED: не удалось загрузить страницу плейлиста ({exc.reason})."
    except Exception as exc:
        return None, f"YANDEX_FETCH_FAILED: не удалось загрузить страницу плейлиста ({exc})."


def _playlist_from_remote_html(url: str, owner: str | None = None, kind: str | None = None) -> ImportedPlaylist:
    html_content, warning = _fetch_public_html(url)
    if not html_content:
        return ImportedPlaylist(
            title=None,
            source_service=YANDEX_SERVICE,
            source_url=url,
            tracks=[],
            owner=owner,
            kind=kind,
            warnings=[warning or WARNING_URL_ONLY],
            raw_data={"method_used": "url_fetch", "fetch_url": url},
        )

    login_wall_markers = ("Войдите в аккаунт", "passport.yandex", "auth?origin=")
    if any(marker in html_content for marker in login_wall_markers):
        return ImportedPlaylist(
            title="Войдите в аккаунт",
            source_service=YANDEX_SERVICE,
            source_url=url,
            tracks=[],
            owner=owner,
            kind=kind,
            warnings=[
                "YANDEX_FETCH_LOGIN_WALL: Яндекс Музыка вернула страницу входа вместо публичного плейлиста. "
                "Для полного импорта загрузите сохраненный HTML страницы плейлиста после открытия в браузере."
            ],
            raw_data={"method_used": "url_fetch_login_wall", "fetch_url": url},
        )

    playlist = extract_from_next_data(html_content, source_url=url)
    if playlist is not None and playlist.tracks:
        playlist.owner = playlist.owner or owner
        playlist.kind = playlist.kind or kind
        playlist.raw_data = {
            "method_used": "url_fetch_next_data",
            "fetch_url": url,
            "payload": playlist.raw_data,
        }
        return playlist

    dom_playlist = extract_yandex_tracks_from_dom(html_content, source_url=url)
    dom_playlist.owner = dom_playlist.owner or owner
    dom_playlist.kind = dom_playlist.kind or kind
    dom_playlist.raw_data = {"method_used": "url_fetch_dom", "fetch_url": url}
    if playlist is not None:
        dom_playlist.warnings = playlist.warnings + dom_playlist.warnings
    return dom_playlist


def detect_playlist_input_type(input_text: str) -> str:
    text = (input_text or "").strip()
    if not text:
        return UNKNOWN
    lowered = text.lower()
    if "<iframe" in lowered and "music.yandex" in lowered and "/iframe/playlist/" in lowered:
        return YANDEX_IFRAME
    if "script" in lowered and "__next_data__" in lowered:
        return YANDEX_SAVED_HTML
    if "<html" in lowered and "music.yandex" in lowered:
        return YANDEX_SAVED_HTML
    if _first_yandex_url(text):
        return YANDEX_PLAYLIST_URL
    if "\n" in text or " - " in text:
        return PLAIN_TEXT
    return UNKNOWN


def parse_yandex_iframe(input_html: str) -> ImportedPlaylist:
    soup = soup_from_html(input_html)
    iframe = soup.find("iframe")
    warnings: list[str] = []
    if iframe is None:
        return ImportedPlaylist(
            title=None,
            source_service=YANDEX_SERVICE,
            source_url=None,
            tracks=[],
            warnings=["YANDEX_IFRAME_WITHOUT_SRC: iframe не найден."],
        )

    src = (iframe.get("src") or "").strip()
    if not src:
        warnings.append("YANDEX_IFRAME_WITHOUT_SRC: iframe найден, но src отсутствует.")
    owner, kind, err = _owner_kind_from_url(src)
    if err:
        warnings.append(f"{err}: формат iframe URL не поддержан полностью.")

    playlist_title = None
    profile_name = None
    profile_url = None
    playlist_url = None
    links = iframe.find_all("a") or soup.find_all("a")
    for link in links:
        href = (link.get("href") or "").strip()
        text = link.get_text(" ", strip=True) or None
        if not href:
            continue
        if "/users/" in href:
            profile_name = text
            profile_url = href
        elif "/playlists/" in href and playlist_title is None:
            playlist_title = text
            playlist_url = href

    if playlist_title is None or profile_name is None or playlist_url is None:
        for match in re.finditer(r"<a\s+[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", input_html, flags=re.I | re.S):
            href = match.group(1).strip()
            text = re.sub(r"<[^>]+>", " ", match.group(2))
            text = re.sub(r"\s+", " ", text).strip() or None
            if "/users/" in href and profile_name is None:
                profile_name = text
                profile_url = href
            elif "/playlists/" in href and playlist_title is None:
                playlist_title = text
                playlist_url = href

    api_playlist = _playlist_from_public_api(owner, kind, src or playlist_url or None)
    if api_playlist is not None and api_playlist.tracks:
        api_playlist.title = api_playlist.title or playlist_title
        api_playlist.profile_name = api_playlist.profile_name or profile_name
        api_playlist.profile_url = api_playlist.profile_url or profile_url
        api_playlist.source_url = src or api_playlist.source_url
        api_playlist.warnings = warnings + api_playlist.warnings
        api_playlist.raw_data = {
            "method_used": "iframe_public_api",
            "iframe_url": src or None,
            "payload": api_playlist.raw_data,
        }
        return api_playlist
    if api_playlist is not None:
        warnings.extend(api_playlist.warnings)

    fetch_url = src or playlist_url or _public_playlist_url(owner, kind, src or None)
    if fetch_url:
        fetched = _playlist_from_remote_html(fetch_url, owner=owner, kind=kind)
        if fetched.tracks:
            fetched.title = fetched.title or playlist_title
            fetched.profile_name = fetched.profile_name or profile_name
            fetched.profile_url = fetched.profile_url or profile_url
            fetched.source_url = src or fetched.source_url
            fetched.warnings = warnings + fetched.warnings
            fetched.raw_data = {
                "method_used": "iframe_fetch",
                "iframe_url": src or None,
                "fetch_url": fetch_url,
            }
            return fetched
        warnings.extend(fetched.warnings)

    warnings.append(WARNING_IFRAME_NO_TRACKS)
    return ImportedPlaylist(
        title=playlist_title,
        source_service=YANDEX_SERVICE,
        source_url=src or None,
        tracks=[],
        owner=owner,
        kind=kind,
        profile_name=profile_name,
        profile_url=profile_url,
        warnings=warnings,
        raw_data={"method_used": "iframe", "iframe_url": src or None, "fetch_url": fetch_url},
    )


def parse_yandex_playlist_url(url: str) -> ImportedPlaylist:
    detected_url = _first_yandex_url(url) or url.strip()
    owner, kind, err = _owner_kind_from_url(detected_url)
    api_playlist = _playlist_from_public_api(owner, kind, detected_url)
    if api_playlist is not None and api_playlist.tracks:
        if err:
            api_playlist.warnings.append(f"{err}: owner/kind РЅРµ СѓРґР°Р»РѕСЃСЊ РёР·РІР»РµС‡СЊ РёР· URL.")
        return api_playlist

    warnings_from_api = api_playlist.warnings[:] if api_playlist is not None else []
    fetch_url = _public_playlist_url(owner, kind, detected_url)
    fetched = _playlist_from_remote_html(fetch_url or detected_url, owner=owner, kind=kind)
    if fetched.tracks:
        if err:
            fetched.warnings.append(f"{err}: owner/kind не удалось извлечь из URL.")
        return fetched

    warnings = [WARNING_URL_ONLY]
    warnings.extend(warnings_from_api)
    warnings.extend(fetched.warnings)
    if err:
        warnings.append(f"{err}: owner/kind не удалось извлечь из URL.")
    return ImportedPlaylist(
        title=fetched.title,
        source_service=YANDEX_SERVICE,
        source_url=detected_url or None,
        tracks=[],
        owner=owner,
        kind=kind,
        warnings=warnings,
        raw_data={"method_used": "url", "fetch_url": fetch_url or detected_url},
    )


def import_yandex_playlist_from_url(url: str) -> ImportedPlaylist:
    """Import a Yandex Music playlist from a public URL without authentication."""
    return parse_yandex_playlist_url(url)


def _safe_get(data: Any, *keys: str) -> Any:
    cur = data
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _iter_query_state_data(next_data: dict[str, Any]) -> list[dict[str, Any]]:
    queries = _safe_get(next_data, "props", "pageProps", "dehydratedState", "queries")
    if not isinstance(queries, list):
        return []
    out: list[dict[str, Any]] = []
    for query in queries:
        state_data = _safe_get(query, "state", "data")
        if isinstance(state_data, dict):
            out.append(state_data)
    return out


def _find_tracks_payload(data: Any) -> dict[str, Any] | None:
    if isinstance(data, dict):
        tracks = data.get("tracks")
        if isinstance(tracks, list) and tracks:
            return data
        for key in ("playlist", "data", "initialData", "pageData"):
            nested = data.get(key)
            found = _find_tracks_payload(nested)
            if found is not None:
                return found
        for value in data.values():
            found = _find_tracks_payload(value)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_tracks_payload(item)
            if found is not None:
                return found
    return None


def _extract_album(track: dict[str, Any]) -> str | None:
    album = track.get("album")
    if isinstance(album, dict):
        return album.get("title") or album.get("name")
    albums = track.get("albums")
    if isinstance(albums, list) and albums:
        first = albums[0]
        if isinstance(first, dict):
            return first.get("title") or first.get("name")
    return None


def yandex_track_to_imported_track(
    track: dict[str, Any],
    playlist: ImportedPlaylist,
) -> ImportedTrack:
    raw_track = track.get("track") if isinstance(track.get("track"), dict) else track
    title = str(raw_track.get("title") or raw_track.get("name") or "").strip()
    artists_raw = raw_track.get("artists") or []
    artist_names: list[str] = []
    if isinstance(artists_raw, list):
        for artist in artists_raw:
            if isinstance(artist, dict):
                name = artist.get("name") or artist.get("title")
            else:
                name = str(artist)
            if name:
                artist_names.append(str(name).strip())
    artist = ", ".join(name for name in artist_names if name) or None
    duration_ms = raw_track.get("durationMs") or raw_track.get("duration_ms")
    try:
        duration_ms = int(duration_ms) if duration_ms is not None else None
    except (TypeError, ValueError):
        duration_ms = None
    source_track_id = raw_track.get("id") or raw_track.get("realId") or raw_track.get("trackId")
    version = None
    if isinstance(raw_track.get("version"), str):
        version = raw_track.get("version")

    return ImportedTrack(
        artist=artist,
        title=title,
        duration_ms=duration_ms,
        album=_extract_album(raw_track),
        source_service=YANDEX_SERVICE,
        source_playlist_title=playlist.title,
        source_playlist_url=playlist.source_url,
        source_track_id=str(source_track_id) if source_track_id is not None else None,
        source_url=None,
        explicit=raw_track.get("explicit") if isinstance(raw_track.get("explicit"), bool) else None,
        version=version,
        raw_data=raw_track,
    )


def _playlist_from_payload(payload: dict[str, Any], source_url: str | None, raw_data: dict[str, Any]) -> ImportedPlaylist:
    title = payload.get("title") or payload.get("name") or "Плейлист"
    owner_value = payload.get("owner")
    owner_login = owner_value.get("login") if isinstance(owner_value, dict) else owner_value
    playlist = ImportedPlaylist(
        title=str(title) if title is not None else None,
        source_service=YANDEX_SERVICE,
        source_url=source_url,
        tracks=[],
        owner=str(owner_login) if owner_login else None,
        kind=str(payload.get("kind")) if payload.get("kind") else None,
        raw_data=raw_data,
    )
    tracks_raw = payload.get("tracks") or []
    if isinstance(tracks_raw, dict):
        tracks_raw = tracks_raw.get("items") or tracks_raw.get("data") or []
    for item in tracks_raw:
        if isinstance(item, dict):
            imported = yandex_track_to_imported_track(item, playlist)
            if imported.title:
                playlist.tracks.append(imported)
    return playlist


def extract_from_next_data(html_content: str, source_url: str | None = None) -> ImportedPlaylist | None:
    soup = soup_from_html(html_content)
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None:
        return None
    script_text = script.string or script.get_text("", strip=True)
    if not script_text:
        return None
    try:
        next_data = json.loads(script_text)
    except json.JSONDecodeError:
        return ImportedPlaylist(
            title=None,
            source_service=YANDEX_SERVICE,
            source_url=source_url,
            tracks=[],
            warnings=["NEXT_DATA_JSON_INVALID: __NEXT_DATA__ найден, но JSON не читается."],
            raw_data={"method_used": "next_data"},
        )

    for state_data in _iter_query_state_data(next_data):
        payload = _find_tracks_payload(state_data)
        if payload is not None:
            playlist = _playlist_from_payload(payload, source_url, payload)
            playlist.raw_data = {"method_used": "next_data", "payload": payload}
            return playlist

    return ImportedPlaylist(
        title=None,
        source_service=YANDEX_SERVICE,
        source_url=source_url,
        tracks=[],
        warnings=["NEXT_DATA_TRACKS_NOT_FOUND: __NEXT_DATA__ найден, но tracks отсутствует."],
        raw_data={"method_used": "next_data"},
    )


TITLE_SELECTORS = [
    ".Meta_title__GGBnH",
    "[class*='Meta_title']",
    "[data-test-id*='title']",
    ".d-track__name",
    "[class*='track'][class*='title']",
]
ARTIST_SELECTORS = [
    ".Meta_artistCaption__JESZi",
    "[class*='Meta_artistCaption']",
    "[data-test-id*='artist']",
    ".d-track__artists",
    "[class*='artist']",
]
DURATION_SELECTORS = [
    "[class*='duration'] span[aria-hidden='true']",
    "[class*='duration']",
    ".d-track__info",
]
ROW_SELECTORS = [
    "[class*='Track_root']",
    "[class*='track__']",
    ".d-track",
    "[data-test-id*='track']",
    "li",
]


def _select_first_text(node, selectors: list[str]) -> str | None:
    for selector in selectors:
        try:
            found = node.select_one(selector)
        except Exception:
            continue
        if found:
            text = found.get_text(" ", strip=True)
            if text:
                return text
    return None


def _dom_playlist_title(soup) -> str | None:
    for selector in ("h1", "[class*='Title']", "[class*='title']"):
        node = soup.select_one(selector)
        if node:
            text = node.get_text(" ", strip=True)
            if text:
                return text
    return None


def extract_yandex_tracks_from_dom(html_content: str, source_url: str | None = None) -> ImportedPlaylist:
    soup = soup_from_html(html_content)
    playlist = ImportedPlaylist(
        title=_dom_playlist_title(soup) or "Плейлист",
        source_service=YANDEX_SERVICE,
        source_url=source_url,
        tracks=[],
        warnings=[WARNING_DOM_PARTIAL],
        raw_data={"method_used": "dom"},
    )
    seen: set[tuple[str | None, str]] = set()
    rows = []
    for selector in ROW_SELECTORS:
        try:
            rows.extend(soup.select(selector))
        except Exception:
            continue
    for row in rows:
        title = _select_first_text(row, TITLE_SELECTORS)
        artist = _select_first_text(row, ARTIST_SELECTORS)
        duration = _select_first_text(row, DURATION_SELECTORS)
        if not title:
            text = row.get_text(" ", strip=True)
            parts = [p.strip() for p in re.split(r"\s{2,}| - ", text) if p.strip()]
            if len(parts) >= 2:
                artist = artist or parts[0]
                title = parts[1]
        if not title:
            continue
        key = (artist, title)
        if key in seen:
            continue
        seen.add(key)
        playlist.tracks.append(
            ImportedTrack(
                artist=artist,
                title=title,
                duration_ms=parse_duration_to_ms(duration),
                source_service=YANDEX_SERVICE,
                source_playlist_title=playlist.title,
                source_playlist_url=source_url,
                raw_data={"method_used": "dom", "duration": duration},
            )
        )
    if not playlist.tracks:
        playlist.warnings.append("DOM_TRACKS_NOT_FOUND: треки в DOM не найдены.")
    return playlist


def import_yandex_saved_html(html_path: str) -> ImportedPlaylist:
    html_content = read_text_file(html_path)
    source_url = str(Path(html_path))
    playlist = extract_from_next_data(html_content, source_url=source_url)
    if playlist is not None and playlist.tracks:
        return playlist
    dom_playlist = extract_yandex_tracks_from_dom(html_content, source_url=source_url)
    if playlist is not None:
        dom_playlist.warnings = playlist.warnings + dom_playlist.warnings
    return dom_playlist


class YandexMusicImporter(PlaylistImporter):
    name = YANDEX_SERVICE

    def can_handle_text(self, input_text: str) -> bool:
        return detect_playlist_input_type(input_text) in {
            YANDEX_IFRAME,
            YANDEX_PLAYLIST_URL,
            YANDEX_SAVED_HTML,
        }

    def import_from_text(self, input_text: str) -> ImportedPlaylist:
        input_type = detect_playlist_input_type(input_text)
        if input_type == YANDEX_IFRAME:
            return parse_yandex_iframe(input_text)
        if input_type == YANDEX_PLAYLIST_URL:
            return import_yandex_playlist_from_url(input_text)
        if input_type == YANDEX_SAVED_HTML:
            playlist = extract_from_next_data(input_text)
            if playlist is not None and playlist.tracks:
                return playlist
            dom_playlist = extract_yandex_tracks_from_dom(input_text)
            if playlist is not None:
                dom_playlist.warnings = playlist.warnings + dom_playlist.warnings
            return dom_playlist
        raise ValueError("UNKNOWN_INPUT_TYPE")

    def can_handle_file(self, file_path: str) -> bool:
        suffix = Path(file_path).suffix.lower()
        return suffix in {".html", ".htm"} or Path(file_path).is_file()

    def import_from_file(self, file_path: str) -> ImportedPlaylist:
        text = read_text_file(file_path)
        input_type = detect_playlist_input_type(text)
        if input_type in {YANDEX_SAVED_HTML, YANDEX_IFRAME, YANDEX_PLAYLIST_URL}:
            if input_type == YANDEX_SAVED_HTML:
                return import_yandex_saved_html(file_path)
            return self.import_from_text(text)
        return TextPlaylistImporter().import_from_text(text)


def import_playlist(content: str | None = None, file_path: str | None = None) -> ImportedPlaylist:
    if file_path:
        return YandexMusicImporter().import_from_file(file_path)
    text = content or ""
    input_type = detect_playlist_input_type(text)
    if input_type == PLAIN_TEXT:
        return TextPlaylistImporter().import_from_text(text)
    if input_type == UNKNOWN:
        return ImportedPlaylist(
            title=None,
            source_service="unknown",
            source_url=None,
            tracks=[],
            warnings=["UNKNOWN_INPUT_TYPE: не удалось распознать формат входных данных."],
        )
    return YandexMusicImporter().import_from_text(text)


def _next_import_dir(root: Path | None = None) -> Path:
    root = root or IMPORT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    base = datetime.now().strftime("import_%Y%m%d_%H%M%S")
    candidate = root / base
    if not candidate.exists():
        return candidate
    for i in range(1, 1000):
        candidate = root / f"{base}_{i}"
        if not candidate.exists():
            return candidate
    return root / f"{base}_{datetime.now().microsecond}"


def save_imported_playlist(playlist: ImportedPlaylist, output_dir: str | Path | None = None) -> dict[str, str]:
    out_dir = Path(output_dir) if output_dir else _next_import_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    txt_path = out_dir / "imported_playlist.txt"
    json_path = out_dir / "imported_playlist.json"
    csv_path = out_dir / "imported_playlist.csv"
    warnings_path = out_dir / "import_warnings.txt"

    txt_path.write_text("\n".join(playlist.to_text_lines()) + ("\n" if playlist.tracks else ""), encoding="utf-8")
    json_path.write_text(json.dumps(playlist.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "index",
                "artist",
                "title",
                "duration_ms",
                "duration",
                "album",
                "source_service",
                "source_track_id",
                "source_url",
            ],
        )
        writer.writeheader()
        for idx, track in enumerate(playlist.tracks, 1):
            writer.writerow(
                {
                    "index": idx,
                    "artist": track.artist or "",
                    "title": track.title,
                    "duration_ms": track.duration_ms or "",
                    "duration": format_duration_ms(track.duration_ms),
                    "album": track.album or "",
                    "source_service": track.source_service or "",
                    "source_track_id": track.source_track_id or "",
                    "source_url": track.source_url or track.source_playlist_url or "",
                }
            )

    warnings_path.write_text(
        "\n".join(f"- {w}" for w in playlist.warnings) + ("\n" if playlist.warnings else ""),
        encoding="utf-8",
    )

    files = {
        "dir": str(out_dir),
        "txt": str(txt_path),
        "json": str(json_path),
        "csv": str(csv_path),
        "warnings": str(warnings_path),
    }
    log.info(
        "playlist import: service=%s title=%r tracks=%s warnings=%s output=%s",
        playlist.source_service,
        playlist.title,
        len(playlist.tracks),
        len(playlist.warnings),
        out_dir,
    )
    return files
