from __future__ import annotations

import re
from typing import Any

from .base import ImportedTrack


_PROD_BY_RE = re.compile(r"\bprod\.?\s*by\b", re.I)
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_UNUSUAL_SYMBOL_RE = re.compile(r"[^0-9A-Za-zА-Яа-яЁё\s\-,.&'\"/()\[\]!?:;+_]")
_COVER_RE = re.compile(r"\bcover\b", re.I)
_SLOWED_RE = re.compile(r"\bslowed\b|\bsped\s*up\b|\bspeed\s*up\b", re.I)
_REMIX_RE = re.compile(r"\bremix\b|\bmix\b", re.I)


def analyze_preview_tracks(tracks: list[ImportedTrack]) -> list[dict[str, Any]]:
    normalized_counts: dict[str, int] = {}
    lines = [track.to_playlist_line() for track in tracks]
    for line in lines:
        key = _normalize_duplicate_key(line)
        if not key:
            continue
        normalized_counts[key] = normalized_counts.get(key, 0) + 1

    payloads: list[dict[str, Any]] = []
    for index, track in enumerate(tracks):
        line = lines[index]
        warnings = _warnings_for_track(track, line, normalized_counts)
        payloads.append(
            {
                "artist": track.artist,
                "title": track.title,
                "duration_ms": track.duration_ms,
                "line": line,
                "warnings": warnings,
            }
        )
    return payloads


def _normalize_duplicate_key(line: str) -> str:
    lowered = (line or "").strip().casefold()
    lowered = re.sub(r"\s+", " ", lowered)
    return lowered


def _warnings_for_track(track: ImportedTrack, line: str, normalized_counts: dict[str, int]) -> list[str]:
    warnings: list[str] = []
    artist = (track.artist or "").strip()
    full_title = _full_title(track)
    title = full_title.strip()
    lowered_line = line.casefold()

    if not artist:
        warnings.append("empty_artist")
    if not title:
        warnings.append("empty_title")
    if " - " in title:
        warnings.append("extra_dash_in_title")
    if _COVER_RE.search(title):
        warnings.append("explicit_version_cover")
    if _SLOWED_RE.search(title):
        warnings.append("explicit_version_slowed")
    if _REMIX_RE.search(title):
        warnings.append("explicit_version_remix")
    if _PROD_BY_RE.search(lowered_line):
        warnings.append("contains_prod_by")
    if _CJK_RE.search(line):
        warnings.append("contains_cjk_symbols")
    if _UNUSUAL_SYMBOL_RE.search(line):
        warnings.append("contains_unusual_symbols")
    if normalized_counts.get(_normalize_duplicate_key(line), 0) > 1:
        warnings.append("possible_duplicate")

    cleaned_title = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", title)
    cleaned_title = re.sub(r"\s+", " ", cleaned_title).strip(" -_")
    if cleaned_title and len(cleaned_title) < 3:
        warnings.append("short_title")

    return warnings


def _full_title(track: ImportedTrack) -> str:
    title = (track.title or "").strip()
    version = (track.version or "").strip()
    if version and version.casefold() not in title.casefold():
        return f"{title} ({version})".strip()
    return title
