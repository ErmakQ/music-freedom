from __future__ import annotations

import unittest

from playlist_utils import decode_playlist_bytes, normalize_playlist_line


class PlaylistUtilsTests(unittest.TestCase):
    def test_decode_playlist_bytes_strips_utf8_bom(self) -> None:
        lines = decode_playlist_bytes("\ufeffArtist - Title\nSecond - Track".encode("utf-8-sig"))

        self.assertEqual(lines[0], "Artist - Title")
        self.assertEqual(lines[1], "Second - Track")

    def test_normalize_playlist_line_removes_number_and_duration(self) -> None:
        self.assertEqual(
            normalize_playlist_line("1. Neverlove - Лисий-кисий [2:57]"),
            "Neverlove - Лисий-кисий",
        )
        self.assertEqual(
            normalize_playlist_line("22) Artist - Title (03:12)"),
            "Artist - Title",
        )


if __name__ == "__main__":
    unittest.main()
