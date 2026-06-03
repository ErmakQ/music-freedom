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
    import_yandex_playlist_from_url,
    import_playlist,
    parse_yandex_iframe,
    parse_yandex_playlist_url,
    save_imported_playlist,
)
from playlist_importers.base import ImportedTrack
from playlist_importers.preview_diagnostics import analyze_preview_tracks
from unittest.mock import patch

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

    @patch("playlist_importers.yandex_music._playlist_from_remote_html")
    @patch("playlist_importers.yandex_music._playlist_from_public_api")
    def test_parse_yandex_iframe(self, mocked_public_api, mocked_remote_html) -> None:
        mocked_public_api.return_value = None
        mocked_remote_html.return_value = import_playlist(content="")
        playlist = parse_yandex_iframe(IFRAME_HTML)
        self.assertEqual(playlist.source_service, "yandex_music")
        self.assertEqual(playlist.owner, "max.ipatow")
        self.assertEqual(playlist.kind, "3")
        self.assertEqual(playlist.title, "Мне нравится")
        self.assertEqual(playlist.profile_name, "Максим Ипатов")
        self.assertEqual(playlist.tracks, [])
        self.assertTrue(playlist.warnings)

    @patch("playlist_importers.yandex_music._playlist_from_remote_html")
    @patch("playlist_importers.yandex_music._playlist_from_public_api")
    def test_parse_iframe_url(self, mocked_public_api, mocked_remote_html) -> None:
        mocked_public_api.return_value = None
        mocked_remote_html.return_value = import_playlist(content="")
        playlist = parse_yandex_playlist_url("https://music.yandex.ru/iframe/playlist/max.ipatow/3")
        self.assertEqual(playlist.owner, "max.ipatow")
        self.assertEqual(playlist.kind, "3")
        self.assertTrue(playlist.warnings)

    @patch("playlist_importers.yandex_music._playlist_from_remote_html")
    @patch("playlist_importers.yandex_music._playlist_from_public_api")
    def test_parse_user_playlist_url(self, mocked_public_api, mocked_remote_html) -> None:
        mocked_public_api.return_value = None
        mocked_remote_html.return_value = import_playlist(content="")
        playlist = parse_yandex_playlist_url("https://music.yandex.ru/users/max.ipatow/playlists/3")
        self.assertEqual(playlist.owner, "max.ipatow")
        self.assertEqual(playlist.kind, "3")

    @patch("playlist_importers.yandex_music._playlist_from_remote_html")
    @patch("playlist_importers.yandex_music._playlist_from_public_api")
    def test_parse_uuid_playlist_url(self, mocked_public_api, mocked_remote_html) -> None:
        mocked_public_api.return_value = None
        mocked_remote_html.return_value = import_playlist(content="")
        playlist = parse_yandex_playlist_url("https://music.yandex.ru/playlists/lk.83f17fa0")
        self.assertIsNone(playlist.owner)
        self.assertEqual(playlist.kind, "lk.83f17fa0")
        self.assertTrue(any("YANDEX_URL_UNSUPPORTED_FORMAT" in w for w in playlist.warnings))

    @patch("playlist_importers.yandex_music._playlist_from_public_api")
    def test_import_yandex_playlist_from_url(self, mocked_public_api) -> None:
        mocked_public_api.return_value = import_playlist(content=next_data_html())
        playlist = import_yandex_playlist_from_url("https://music.yandex.ru/users/max.ipatow/playlists/3")
        self.assertEqual(playlist.source_service, "yandex_music")
        self.assertEqual(len(playlist.tracks), 1)
        self.assertEqual(playlist.tracks[0].artist, "Artist")
        self.assertEqual(playlist.tracks[0].title, "Song")

    @patch("playlist_importers.yandex_music._fetch_public_json")
    def test_import_yandex_playlist_from_public_api(self, mocked_fetch_public_json) -> None:
        mocked_fetch_public_json.return_value = (
            {
                "result": {
                    "title": "Мне нравится",
                    "kind": 3,
                    "owner": {"login": "max.ipatow", "name": "Максим Ипатов"},
                    "tracks": [
                        {
                            "track": {
                                "id": "1",
                                "title": "Song",
                                "durationMs": 213000,
                                "artists": [{"name": "Artist"}],
                                "albums": [{"title": "Album"}],
                            }
                        }
                    ],
                    "pager": {"total": 1, "page": 0, "perPage": 100},
                }
            },
            None,
        )
        playlist = import_yandex_playlist_from_url("https://music.yandex.ru/users/max.ipatow/playlists/3")
        self.assertEqual(playlist.title, "Мне нравится")
        self.assertEqual(playlist.owner, "max.ipatow")
        self.assertEqual(playlist.kind, "3")
        self.assertEqual(playlist.profile_name, "Максим Ипатов")
        self.assertEqual(len(playlist.tracks), 1)
        self.assertEqual(playlist.tracks[0].artist, "Artist")
        self.assertEqual(playlist.tracks[0].title, "Song")

    @patch("playlist_importers.yandex_music._fetch_public_json")
    def test_import_yandex_playlist_from_public_api_keeps_fetching_after_short_page(self, mocked_fetch_public_json) -> None:
        pages = {
            0: {"result": {"title": "Мне нравится", "kind": 3, "owner": {"login": "max.ipatow", "name": "Максим Ипатов"}, "tracks": [{"track": {"id": "1", "title": "Song 1", "artists": [{"name": "Artist"}]}}], "pager": {"total": 3, "page": 0, "perPage": 1}}},
            1: {"result": {"title": "Мне нравится", "kind": 3, "owner": {"login": "max.ipatow", "name": "Максим Ипатов"}, "tracks": [{"track": {"id": "2", "title": "Song 2", "artists": [{"name": "Artist"}]}}], "pager": {"total": 3, "page": 1, "perPage": 2}}},
            2: {"result": {"title": "Мне нравится", "kind": 3, "owner": {"login": "max.ipatow", "name": "Максим Ипатов"}, "tracks": [{"track": {"id": "3", "title": "Song 3", "artists": [{"name": "Artist"}]}}], "pager": {"total": 3, "page": 2, "perPage": 2}}},
        }

        def fake_fetch(url: str):
            page_part = url.split("page=")[1].split("&")[0]
            page = int(page_part)
            return pages.get(page), None

        mocked_fetch_public_json.side_effect = fake_fetch
        playlist = import_yandex_playlist_from_url("https://music.yandex.ru/users/max.ipatow/playlists/3")
        self.assertEqual(len(playlist.tracks), 3)
        self.assertEqual([track.title for track in playlist.tracks], ["Song 1", "Song 2", "Song 3"])

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

    def test_plain_text_keeps_extra_dash_in_title(self) -> None:
        playlist = import_playlist(content="Flaffy Fon - Голова - бумага")
        self.assertEqual(len(playlist.tracks), 1)
        self.assertEqual(playlist.tracks[0].artist, "Flaffy Fon")
        self.assertEqual(playlist.tracks[0].title, "Голова - бумага")

    def test_imported_track_to_playlist_line_keeps_explicit_version(self) -> None:
        track = ImportedTrack(artist="EVO", title="Танцуй", version="Cover")
        self.assertEqual(track.to_playlist_line(), "EVO - Танцуй (Cover)")

    def test_preview_diagnostics_flags_versions_and_extra_dash(self) -> None:
        tracks = [
            ImportedTrack(artist="tripperbeats, Flo Fire, LeMoris", title="Ride - DnB"),
            ImportedTrack(artist="EVO", title="Танцуй", version="Cover"),
            ImportedTrack(artist="GERXMVP", title="Olympus (Slowed)"),
            ImportedTrack(artist="BUSHIDO ZHO", title="далеко (большой Бушизм) [prod. by wex & heysubr]"),
        ]
        preview = analyze_preview_tracks(tracks)

        self.assertIn("extra_dash_in_title", preview[0]["warnings"])
        self.assertIn("explicit_version_cover", preview[1]["warnings"])
        self.assertIn("explicit_version_slowed", preview[2]["warnings"])
        self.assertIn("contains_prod_by", preview[3]["warnings"])

    @patch("playlist_importers.yandex_music._playlist_from_remote_html")
    @patch("playlist_importers.yandex_music._playlist_from_public_api")
    def test_iframe_without_tracks_warning(self, mocked_public_api, mocked_remote_html) -> None:
        mocked_public_api.return_value = None
        mocked_remote_html.return_value = import_playlist(content="")
        playlist = parse_yandex_iframe(IFRAME_HTML)
        self.assertEqual(len(playlist.tracks), 0)
        self.assertTrue(any("Iframe распознан" in w for w in playlist.warnings))


if __name__ == "__main__":
    unittest.main()
