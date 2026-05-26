"""Декодирование плейлиста (.txt) для веб-API."""

import os


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
    for enc in ("utf-8", "utf-8-sig", "cp1251"):
        try:
            text = data.decode(enc)
            return [ln.rstrip("\r\n") for ln in text.splitlines()]
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace").splitlines()
