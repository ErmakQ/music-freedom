from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from web.app import app


class ImportPreviewEndpointTests(unittest.TestCase):
    def test_import_preview_returns_line_warnings(self) -> None:
        content = "\n".join(
            [
                "tripperbeats, Flo Fire, LeMoris - Ride - DnB",
                "EVO - Танцуй (Cover)",
                "GERXMVP - Olympus (Slowed)",
                "BUSHIDO ZHO - далеко (большой Бушизм) [prod. by wex & heysubr]",
                "EVO - Танцуй (Cover)",
            ]
        )
        with TestClient(app) as client:
            res = client.post(
                "/api/import-playlist",
                data={
                    "content": content,
                    "run": "false",
                    "delivery": "zip",
                    "speed_profile": "fast",
                    "parallel_tasks": "auto",
                },
            )

        self.assertEqual(res.status_code, 200)
        payload = res.json()
        preview = payload["preview"]
        self.assertIn("extra_dash_in_title", preview[0]["warnings"])
        self.assertIn("explicit_version_cover", preview[1]["warnings"])
        self.assertIn("possible_duplicate", preview[1]["warnings"])
        self.assertIn("explicit_version_slowed", preview[2]["warnings"])
        self.assertIn("contains_prod_by", preview[3]["warnings"])


if __name__ == "__main__":
    unittest.main()
