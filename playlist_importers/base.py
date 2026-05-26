from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


PLAIN_TEXT = "PLAIN_TEXT"
YANDEX_IFRAME = "YANDEX_IFRAME"
YANDEX_PLAYLIST_URL = "YANDEX_PLAYLIST_URL"
YANDEX_SAVED_HTML = "YANDEX_SAVED_HTML"
UNKNOWN = "UNKNOWN"


@dataclass
class ImportedTrack:
    artist: str | None
    title: str
    duration_ms: int | None = None
    album: str | None = None
    source_service: str | None = None
    source_playlist_title: str | None = None
    source_playlist_url: str | None = None
    source_track_id: str | None = None
    source_url: str | None = None
    explicit: bool | None = None
    version: str | None = None
    raw_data: dict[str, Any] | None = None

    def to_playlist_line(self) -> str:
        artist = (self.artist or "").strip()
        title = self.title.strip()
        return f"{artist} - {title}" if artist else title

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ImportedPlaylist:
    title: str | None
    source_service: str
    source_url: str | None
    tracks: list[ImportedTrack]
    owner: str | None = None
    kind: str | None = None
    profile_name: str | None = None
    profile_url: str | None = None
    warnings: list[str] = field(default_factory=list)
    raw_data: dict[str, Any] | None = None

    def to_text_lines(self) -> list[str]:
        return [track.to_playlist_line() for track in self.tracks if track.title.strip()]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["tracks"] = [track.to_dict() for track in self.tracks]
        return data


class PlaylistImporter:
    name: str = "base"

    def can_handle_text(self, input_text: str) -> bool:
        return False

    def import_from_text(self, input_text: str) -> ImportedPlaylist:
        raise NotImplementedError

    def can_handle_file(self, file_path: str) -> bool:
        return False

    def import_from_file(self, file_path: str) -> ImportedPlaylist:
        raise NotImplementedError
