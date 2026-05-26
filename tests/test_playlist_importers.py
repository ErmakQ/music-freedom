from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from playlist_importers import (
    PLAIN_TEXT,
    YANDEX_IFRAME,
    YANDEX_PLAYLIST_URL,
    YANDEX_SAVED_HTML,
    detect_playlist_input_type,
    import_playlist,
    parse_yandex_iframe,
    parse_yandex_playlist_url,
    save_imported_playlist,
)
from playlist_importers.yandex_music import extract_from_next_data, extract_yandex_tracks_from_dom


IFRAME_HTML = """
<iframe src="https://music.yandex.ru/iframe/playlist/max.ipatow/3">
  Слушайте <a href="https://music.yandex.ru/playlists/lk.xxx">Мне нравится</a>
  — <a href="https://music.yandex.ru/users/max.ipatow">Максим Ипатов</a>
</iframe>
"""


def next_data_html() -> str:
    data = {
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [
                        {
                            "state": {
                                "data": {
                                    "title": "Мне нравится",
                                    "tracks": [
                                        {
                                            "title": "Song",
                                            "durationMs": 213000,
                                            "id": "123",
                                            "artists": [{"name": "Artist"}],
                                            "albums": [{"title": "Album"}],
                                        }
                                    ],
                                }
                            }
                        }
                    ]
                }
            }
        }
    }
    return f"""
    <html>
      <script id="__NEXT_DATA__" type="application/json">
      {json.dumps(data, ensure_ascii=False)}
      </script>
    </html>
    """


class PlaylistImporterTests(unittest.TestCase):
    def test_detect_input_types(self) -> None:
        self.assertEqual(detect_playlist_input_type(IFRAME_HTML), YANDEX_IFRAME)
        self.assertEqual(
            detect_playlist_input_type("https://music.yandex.ru/users/max.ipatow/playlists/3"),
            YANDEX_PLAYLIST_URL,
        )
        self.assertEqual(detect_playlist_input_type(next_data_html()), YANDEX_SAVED_HTML)
        self.assertEqual(detect_playlist_input_type("Artist - Title\nArtist2 - Title2"), PLAIN_TEXT)

    def test_parse_yandex_iframe(self) -> None:
        playlist = parse_yandex_iframe(IFRAME_HTML)
        self.assertEqual(playlist.source_service, "yandex_music")
        self.assertEqual(playlist.owner, "max.ipatow")
        self.assertEqual(playlist.kind, "3")
        self.assertEqual(playlist.title, "Мне нравится")
        self.assertEqual(playlist.profile_name, "Максим Ипатов")
        self.assertEqual(playlist.tracks, [])
        self.assertTrue(playlist.warnings)

    def test_parse_iframe_url(self) -> None:
        playlist = parse_yandex_playlist_url("https://music.yandex.ru/iframe/playlist/max.ipatow/3")
        self.assertEqual(playlist.owner, "max.ipatow")
        self.assertEqual(playlist.kind, "3")
        self.assertTrue(playlist.warnings)

    def test_parse_user_playlist_url(self) -> None:
        playlist = parse_yandex_playlist_url("https://music.yandex.ru/users/max.ipatow/playlists/3")
        self.assertEqual(playlist.owner, "max.ipatow")
        self.assertEqual(playlist.kind, "3")

    def test_parse_uuid_playlist_url(self) -> None:
        playlist = parse_yandex_playlist_url("https://music.yandex.ru/playlists/lk.83f17fa0")
        self.assertIsNone(playlist.owner)
        self.assertEqual(playlist.kind, "lk.83f17fa0")
        self.assertTrue(any("YANDEX_URL_UNSUPPORTED_FORMAT" in w for w in playlist.warnings))

    def test_extract_tracks_from_next_data(self) -> None:
        playlist = extract_from_next_data(next_data_html())
        self.assertIsNotNone(playlist)
        assert playlist is not None
        self.assertEqual(playlist.title, "Мне нравится")
        self.assertEqual(len(playlist.tracks), 1)
        self.assertEqual(playlist.tracks[0].artist, "Artist")
        self.assertEqual(playlist.tracks[0].title, "Song")
        self.assertEqual(playlist.tracks[0].duration_ms, 213000)
        self.assertEqual(playlist.tracks[0].album, "Album")
        self.assertEqual(playlist.tracks[0].source_track_id, "123")

    def test_dom_fallback(self) -> None:
        html = """
        <html><body>
          <h1>DOM Playlist</h1>
          <div class="d-track">
            <span class="d-track__artists">Artist</span>
            <span class="d-track__name">Title</span>
            <span class="duration">3:45</span>
          </div>
        </body></html>
        """
        playlist = extract_yandex_tracks_from_dom(html)
        self.assertEqual(playlist.title, "DOM Playlist")
        self.assertEqual(len(playlist.tracks), 1)
        self.assertEqual(playlist.tracks[0].artist, "Artist")
        self.assertEqual(playlist.tracks[0].title, "Title")
        self.assertEqual(playlist.tracks[0].duration_ms, 225000)
        self.assertTrue(playlist.warnings)

    def test_save_imported_playlist_files(self) -> None:
        playlist = import_playlist(content=next_data_html())
        with tempfile.TemporaryDirectory() as td:
            files = save_imported_playlist(playlist, td)
            txt = Path(files["txt"]).read_text(encoding="utf-8")
            data = json.loads(Path(files["json"]).read_text(encoding="utf-8"))
            csv_text = Path(files["csv"]).read_text(encoding="utf-8")
        self.assertIn("Artist - Song", txt)
        self.assertEqual(data["tracks"][0]["title"], "Song")
        self.assertIn("duration_ms", csv_text)

    def test_bad_json_warning(self) -> None:
        playlist = extract_from_next_data('<script id="__NEXT_DATA__">{bad</script>')
        self.assertIsNotNone(playlist)
        assert playlist is not None
        self.assertEqual(len(playlist.tracks), 0)
        self.assertTrue(any("NEXT_DATA_JSON_INVALID" in w for w in playlist.warnings))

    def test_empty_input_warning(self) -> None:
        playlist = import_playlist(content="")
        self.assertEqual(playlist.source_service, "unknown")
        self.assertEqual(len(playlist.tracks), 0)
        self.assertTrue(any("UNKNOWN_INPUT_TYPE" in w for w in playlist.warnings))

    def test_plain_text_bom_is_stripped(self) -> None:
        playlist = import_playlist(content="\ufeffArtist - Song\nSecond Artist - Track")
        self.assertEqual(len(playlist.tracks), 2)
        self.assertEqual(playlist.tracks[0].artist, "Artist")
        self.assertEqual(playlist.tracks[0].title, "Song")

    def test_iframe_without_tracks_warning(self) -> None:
        playlist = parse_yandex_iframe(IFRAME_HTML)
        self.assertEqual(len(playlist.tracks), 0)
        self.assertTrue(any("Iframe распознан" in w for w in playlist.warnings))


if __name__ == "__main__":
    unittest.main()
