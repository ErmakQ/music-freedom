from __future__ import annotations

from .base import ImportedPlaylist, ImportedTrack, PlaylistImporter


class TextPlaylistImporter(PlaylistImporter):
    name = "plain_text"

    def can_handle_text(self, input_text: str) -> bool:
        return bool((input_text or "").strip())

    def import_from_text(self, input_text: str) -> ImportedPlaylist:
        tracks: list[ImportedTrack] = []
        for line in (input_text or "").splitlines():
            line = line.strip().lstrip("\ufeff")
            if not line:
                continue
            artist = None
            title = line
            if " - " in line:
                artist, title = line.rsplit(" - ", 1)
                artist = artist.strip() or None
                title = title.strip()
            tracks.append(
                ImportedTrack(
                    artist=artist,
                    title=title,
                    source_service="plain_text",
                    raw_data={"line": line},
                )
            )
        return ImportedPlaylist(
            title="Imported playlist",
            source_service="plain_text",
            source_url=None,
            tracks=tracks,
        )

    def can_handle_file(self, file_path: str) -> bool:
        return file_path.lower().endswith(".txt")

    def import_from_file(self, file_path: str) -> ImportedPlaylist:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            return self.import_from_text(f.read())
