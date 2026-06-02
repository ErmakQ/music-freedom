from __future__ import annotations

import re
from pathlib import Path

from bs4 import BeautifulSoup


def read_text_file(file_path: str) -> str:
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"HTML_FILE_NOT_FOUND: {file_path}")
    raw = path.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "cp1251", "windows-1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def soup_from_html(html_content: str) -> BeautifulSoup:
    return BeautifulSoup(html_content or "", "html.parser")


def parse_duration_to_ms(value: str | None) -> int | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    parts = [p for p in re.split(r"[:.]", text) if p.isdigit()]
    if len(parts) == 2:
        minutes, seconds = map(int, parts)
        return (minutes * 60 + seconds) * 1000
    if len(parts) == 3:
        hours, minutes, seconds = map(int, parts)
        return (hours * 3600 + minutes * 60 + seconds) * 1000
    return None


def format_duration_ms(duration_ms: int | None) -> str:
    if duration_ms is None:
        return ""
    total_seconds = max(0, int(round(duration_ms / 1000)))
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"
