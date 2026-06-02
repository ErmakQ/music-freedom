"""Декодирование плейлиста (.txt) для веб-API."""

import os
import re


def effective_max_playlist_lines() -> int | None:
    """
    Максимум непустых строк в одном файле.

    None — без лимита (переменная не задана, пустая или <= 0).
    Положительное число — жёсткий потолок (MAX_PLAYLIST_LINES в .env).
    """
    raw = os.getenv("MAX_PLAYLIST_LINES")
    if raw is None or not str(raw).strip():
        return None
    try:
        n = int(str(raw).strip(), 10)
    except ValueError:
        return None
    return None if n <= 0 else n


def decode_playlist_bytes(data: bytes) -> list[str]:
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            text = data.decode(enc)
            return [ln.lstrip("\ufeff").rstrip("\r\n") for ln in text.splitlines()]
        except UnicodeDecodeError:
            continue
    return [ln.lstrip("\ufeff") for ln in data.decode("utf-8", errors="replace").splitlines()]


def normalize_playlist_line(line: str) -> str:
    """
    Приводит пользовательскую строку к формату поиска.

    Частый экспорт плейлистов выглядит как:
    `1. Artist - Title [2:57]`
    Для поиска и quality scoring нужны только `Artist - Title`.
    """
    s = (line or "").strip().lstrip("\ufeff")
    s = re.sub(r"^\s*\d+[\.)]\s+", "", s)
    s = re.sub(r"\s*\[(?:\d{1,2}:)?\d{1,2}:\d{2}\]\s*$", "", s)
    s = re.sub(r"\s*\(\s*(?:\d{1,2}:)?\d{1,2}:\d{2}\s*\)\s*$", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def extract_playlist_duration_sec(line: str) -> int | None:
    match = re.search(r"[\[\(]\s*((?:(\d{1,2}):)?(\d{1,2}):(\d{2}))\s*[\]\)]\s*$", line or "")
    if not match:
        return None
    hours = int(match.group(2) or 0)
    minutes = int(match.group(3))
    seconds = int(match.group(4))
    return hours * 3600 + minutes * 60 + seconds
