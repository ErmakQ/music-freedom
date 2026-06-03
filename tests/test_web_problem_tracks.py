from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import quality
from web.app import app


def candidate(title: str, score: float = 0.7) -> quality.TrackCandidate:
    artist, raw_title = ("Artist", title)
    if " - " in title:
        artist, raw_title = title.split(" - ", 1)
    cand = quality.TrackCandidate(
        provider="hitmo:http",
        input_track="Artist - Song",
        raw_artist=artist,
        raw_title=raw_title,
        raw_display_title=title,
        source_url="https://example.test/source",
        download_url="https://example.test/file.mp3",
    )
    cand.score = score
    return cand


class ProblemTracksEndpointTests(unittest.TestCase):
    def test_problem_tracks_endpoint_reads_quality_reports(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            items = [
                quality.make_quality_item(
                    "Artist - Accepted",
                    quality.STATUS_OK,
                    selected_candidate=candidate("Artist - Accepted", 0.95),
                ),
                quality.make_quality_item(
                    "Artist - Missing",
                    quality.STATUS_NOT_FOUND,
                    reason="provider miss",
                ),
                quality.make_quality_item(
                    "Artist - Radio",
                    quality.STATUS_REJECTED_RADIO_VERSION,
                    reason="radio",
                    selected_candidate=candidate("Artist - Radio Edit", 0.88),
                ),
                quality.make_quality_item(
                    "Artist - Broken",
                    quality.STATUS_DOWNLOAD_FAILED,
                    reason="HTTP 500",
                    selected_candidate=candidate("Artist - Broken", 0.9),
                ),
                quality.make_quality_item(
                    "Artist - Odd",
                    quality.STATUS_SUSPICIOUS_FILE,
                    reason="too small",
                    selected_candidate=candidate("Artist - Odd", 0.9),
                ),
                quality.make_quality_item(
                    "Artist - Weak",
                    quality.STATUS_LOW_CONFIDENCE_MATCH,
                    reason="score below threshold",
                    selected_candidate=candidate("Other - Weak", 0.66),
                ),
            ]
            candidates_report = [
                {
                    "input_track": "Artist - Weak",
                    "decision_reason": "http_only_selenium_disabled",
                    "from_cache": True,
                    "provider_results": [
                        {
                            "provider": "hitmo:http",
                            "status": "OK",
                            "reason": "fixture",
                            "from_cache": False,
                            "candidate_count": 1,
                        }
                    ],
                    "candidates": [quality.candidate_to_dict(candidate("Other - Weak", 0.66))],
                }
            ]
            quality.write_quality_reports(root, items, candidates_report, total=6)

            with TestClient(app) as client:
                job_id = "job_problem_tracks"
                with app.state.jobs_lock:
                    app.state.jobs[job_id] = {
                        "job_id": job_id,
                        "status": "partial_success",
                        "phase": "done",
                        "saved_run": str(root),
                        "work_root": None,
                        "total_lines": 6,
                        "counts": {"ok": 1, "failed": 5, "skipped": 0},
                        "quality_counts": {},
                    }

                res = client.get(f"/api/jobs/{job_id}/problem-tracks")

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["job_id"], "job_problem_tracks")
        self.assertEqual(
            data["summary"],
            {
                "total": 6,
                "accepted": 1,
                "not_found": 1,
                "manual_review": 5,
                "rejected": 1,
                "download_failed": 1,
                "suspicious": 1,
            },
        )
        self.assertEqual(data["groups"]["not_found"][0]["input_line"], "Artist - Missing")
        self.assertEqual(data["groups"]["rejected"][0]["user_status_label"], "Отклонено фильтром")
        self.assertEqual(data["groups"]["download_failed"][0]["reason"], "HTTP 500")
        self.assertEqual(data["groups"]["suspicious"][0]["status"], quality.STATUS_SUSPICIOUS_FILE)
        weak = data["groups"]["manual_review"][0]
        self.assertEqual(weak["artist"], "Artist")
        self.assertEqual(weak["title"], "Weak")
        self.assertEqual(weak["decision_reason"], "http_only_selenium_disabled")
        self.assertTrue(weak["from_cache"])
        self.assertEqual(weak["provider_summary"][0]["provider"], "hitmo:http")
        self.assertEqual(weak["score"], 0.66)
        self.assertEqual(weak["best_candidate"]["source"], "hitmo:http")
        self.assertEqual(weak["best_candidate"]["display_title"], "Other - Weak")

    def test_problem_tracks_endpoint_has_stable_empty_fallback(self) -> None:
        with TestClient(app) as client:
            job_id = "job_empty_problem_tracks"
            with app.state.jobs_lock:
                app.state.jobs[job_id] = {
                    "job_id": job_id,
                    "status": "running",
                    "phase": "phase1",
                    "total_lines": 2,
                    "counts": {"ok": 0, "failed": 0, "skipped": 0},
                    "quality_counts": {},
                    "report": None,
                }

            res = client.get(f"/api/jobs/{job_id}/problem-tracks")

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["summary"]["total"], 2)
        self.assertEqual(set(data["groups"]), {"not_found", "manual_review", "rejected", "download_failed", "suspicious"})
        self.assertTrue(all(group == [] for group in data["groups"].values()))

    def test_not_found_txt_endpoint_reads_only_not_found_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            items = [
                quality.make_quality_item("Artist - Missing 1", quality.STATUS_NOT_FOUND, reason="miss"),
                quality.make_quality_item("Artist - Missing 2", quality.STATUS_NOT_FOUND, reason="miss"),
                quality.make_quality_item("Artist - Weak", quality.STATUS_LOW_CONFIDENCE_MATCH, reason="weak"),
                quality.make_quality_item("Artist - Radio", quality.STATUS_REJECTED_RADIO_VERSION, reason="radio"),
            ]
            quality.write_quality_reports(root, items, [], total=4)

            with TestClient(app) as client:
                job_id = "job_not_found_txt"
                with app.state.jobs_lock:
                    app.state.jobs[job_id] = {
                        "job_id": job_id,
                        "status": "partial_success",
                        "phase": "done",
                        "saved_run": str(root),
                        "work_root": None,
                        "total_lines": 4,
                        "counts": {"ok": 0, "failed": 4, "skipped": 0},
                        "quality_counts": {},
                    }

                res = client.get(f"/api/jobs/{job_id}/not-found.txt")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["content-type"], "text/plain; charset=utf-8")
        self.assertIn('filename="job_not_found_txt-not-found.txt"', res.headers.get("content-disposition", ""))
        self.assertEqual(res.text, "Artist - Missing 1\nArtist - Missing 2\n")

    def test_not_found_txt_endpoint_falls_back_to_current_job_result(self) -> None:
        with TestClient(app) as client:
            job_id = "job_not_found_txt_fallback"
            with app.state.jobs_lock:
                app.state.jobs[job_id] = {
                    "job_id": job_id,
                    "status": "failed",
                    "phase": "done",
                    "total_lines": 2,
                    "counts": {"ok": 0, "failed": 2, "skipped": 0},
                    "quality_counts": {"not_found": 2},
                    "report": {"failed": ["Artist - Missing A", "Artist - Missing B"]},
                }

            res = client.get(f"/api/jobs/{job_id}/not-found.txt")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.text, "Artist - Missing A\nArtist - Missing B\n")

    def test_track_candidates_endpoint_returns_candidates_for_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            weak = candidate("Other - Weak", 0.66)
            weak.flags = ["radio"]
            weak.hard_reject = True
            weak.reject_reason = quality.STATUS_REJECTED_RADIO_VERSION
            weak.duration_sec = 214
            items = [
                quality.make_quality_item(
                    "Artist - Weak",
                    quality.STATUS_LOW_CONFIDENCE_MATCH,
                    reason="score below threshold",
                    selected_candidate=weak,
                ),
            ]
            candidates_report = [
                {
                    "input_track": "Artist - Weak",
                    "decision_reason": "http_only_selenium_disabled",
                    "from_cache": False,
                    "provider_results": [],
                    "candidates": [quality.candidate_to_dict(weak)],
                }
            ]
            quality.write_quality_reports(root, items, candidates_report, total=1)

            with TestClient(app) as client:
                job_id = "job_track_candidates"
                with app.state.jobs_lock:
                    app.state.jobs[job_id] = {
                        "job_id": job_id,
                        "status": "partial_success",
                        "phase": "done",
                        "saved_run": str(root),
                        "work_root": None,
                        "total_lines": 1,
                        "counts": {"ok": 0, "failed": 1, "skipped": 0},
                        "quality_counts": {},
                    }

                problem = client.get(f"/api/jobs/{job_id}/problem-tracks").json()
                track_id = problem["groups"]["manual_review"][0]["track_id"]
                res = client.get(f"/api/jobs/{job_id}/tracks/{track_id}/candidates")

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["job_id"], "job_track_candidates")
        self.assertEqual(data["track_id"], track_id)
        self.assertEqual(data["input_line"], "Artist - Weak")
        self.assertEqual(data["decision_reason"], "http_only_selenium_disabled")
        self.assertEqual(data["best_candidate"]["score"], 0.66)
        self.assertEqual(len(data["candidates"]), 1)
        self.assertEqual(data["candidates"][0]["source"], "hitmo:http")
        self.assertEqual(data["candidates"][0]["duration_sec"], 214)
        self.assertEqual(data["candidates"][0]["version_flags"], ["radio"])
        self.assertEqual(data["candidates"][0]["status"], quality.STATUS_REJECTED_RADIO_VERSION)
        self.assertFalse(data["candidates"][0]["can_preview"])
        self.assertTrue(data["candidates"][0]["can_accept"])
        self.assertIsNone(data["candidates"][0]["downloaded_path"])

    def test_manual_accept_changes_status(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rejected_dir = root / "rejected"
            rejected_dir.mkdir(parents=True)
            rejected_file = rejected_dir / "weak.mp3"
            rejected_file.write_bytes(b"manual-accept")

            weak = candidate("Other - Weak", 0.66)
            items = [
                quality.make_quality_item(
                    "Artist - Weak",
                    quality.STATUS_LOW_CONFIDENCE_MATCH,
                    reason="weak",
                    selected_candidate=weak,
                    file=str(rejected_file),
                ),
            ]
            candidates_report = [
                {
                    "input_track": "Artist - Weak",
                    "decision_reason": "http_only_selenium_disabled",
                    "from_cache": False,
                    "provider_results": [],
                    "candidates": [quality.candidate_to_dict(weak)],
                }
            ]
            quality.write_quality_reports(root, items, candidates_report, total=1)

            with TestClient(app) as client:
                job_id = "job_manual_accept"
                with app.state.jobs_lock:
                    app.state.jobs[job_id] = {
                        "job_id": job_id,
                        "status": "partial_success",
                        "phase": "done",
                        "saved_run": str(root),
                        "work_root": None,
                        "delivery": "zip",
                        "total_lines": 1,
                        "counts": {"ok": 0, "failed": 1, "skipped": 0},
                        "quality_counts": {},
                        "download_ready": True,
                    }

                problem = client.get(f"/api/jobs/{job_id}/problem-tracks").json()
                track = problem["groups"]["manual_review"][0]
                res = client.post(
                    f"/api/jobs/{job_id}/manual-accept",
                    json={
                        "track_id": track["track_id"],
                        "candidate_id": track["best_candidate"]["candidate_id"],
                    },
                )
                updated_problem = client.get(f"/api/jobs/{job_id}/problem-tracks").json()
                decisions_text = root.joinpath("manual_decisions.json").read_text(encoding="utf-8")
                accepted_count = len(list((root / "accepted").iterdir()))

        self.assertEqual(res.status_code, 200)
        self.assertEqual(updated_problem["summary"]["accepted"], 1)
        self.assertEqual(updated_problem["summary"]["manual_review"], 0)
        self.assertEqual(updated_problem["groups"]["manual_review"], [])
        self.assertIn("MANUALLY_ACCEPTED", decisions_text)
        self.assertEqual(accepted_count, 1)

    def test_manual_reject_saves_decision(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            weak = candidate("Other - Weak", 0.66)
            items = [
                quality.make_quality_item(
                    "Artist - Weak",
                    quality.STATUS_LOW_CONFIDENCE_MATCH,
                    reason="weak",
                    selected_candidate=weak,
                ),
            ]
            candidates_report = [
                {
                    "input_track": "Artist - Weak",
                    "decision_reason": "http_only_selenium_disabled",
                    "from_cache": False,
                    "provider_results": [],
                    "candidates": [quality.candidate_to_dict(weak)],
                }
            ]
            quality.write_quality_reports(root, items, candidates_report, total=1)

            with TestClient(app) as client:
                job_id = "job_manual_reject"
                with app.state.jobs_lock:
                    app.state.jobs[job_id] = {
                        "job_id": job_id,
                        "status": "partial_success",
                        "phase": "done",
                        "saved_run": str(root),
                        "work_root": None,
                        "delivery": "zip",
                        "total_lines": 1,
                        "counts": {"ok": 0, "failed": 1, "skipped": 0},
                        "quality_counts": {},
                        "download_ready": True,
                    }

                problem = client.get(f"/api/jobs/{job_id}/problem-tracks").json()
                track = problem["groups"]["manual_review"][0]
                res = client.post(
                    f"/api/jobs/{job_id}/manual-reject",
                    json={
                        "track_id": track["track_id"],
                        "candidate_id": track["best_candidate"]["candidate_id"],
                        "reason": "wrong version",
                    },
                )
                updated_problem = client.get(f"/api/jobs/{job_id}/problem-tracks").json()
                decisions_text = root.joinpath("manual_decisions.json").read_text(encoding="utf-8")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(updated_problem["summary"]["manual_review"], 0)
        self.assertEqual(updated_problem["summary"]["rejected"], 1)
        self.assertEqual(updated_problem["groups"]["rejected"][0]["status"], "MANUALLY_REJECTED")
        self.assertIn("wrong version", decisions_text)

    def test_rebuild_archive_includes_manually_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accepted_dir = root / "accepted"
            rejected_dir = root / "rejected"
            accepted_dir.mkdir(parents=True)
            rejected_dir.mkdir(parents=True)
            (accepted_dir / "ok.mp3").write_bytes(b"ok")
            rejected_file = rejected_dir / "weak.mp3"
            rejected_file.write_bytes(b"manual-accept")

            weak = candidate("Other - Weak", 0.66)
            items = [
                quality.make_quality_item(
                    "Artist - Accepted",
                    quality.STATUS_OK,
                    selected_candidate=candidate("Artist - Accepted", 0.95),
                    file=str(accepted_dir / "ok.mp3"),
                ),
                quality.make_quality_item(
                    "Artist - Weak",
                    quality.STATUS_LOW_CONFIDENCE_MATCH,
                    reason="weak",
                    selected_candidate=weak,
                    file=str(rejected_file),
                ),
            ]
            candidates_report = [
                {
                    "input_track": "Artist - Weak",
                    "decision_reason": "http_only_selenium_disabled",
                    "from_cache": False,
                    "provider_results": [],
                    "candidates": [quality.candidate_to_dict(weak)],
                }
            ]
            quality.write_quality_reports(root, items, candidates_report, total=2)

            with TestClient(app) as client:
                job_id = "job_rebuild_accept"
                with app.state.jobs_lock:
                    app.state.jobs[job_id] = {
                        "job_id": job_id,
                        "status": "partial_success",
                        "phase": "done",
                        "saved_run": str(root),
                        "work_root": None,
                        "delivery": "zip",
                        "total_lines": 2,
                        "counts": {"ok": 1, "failed": 1, "skipped": 0},
                        "quality_counts": {},
                        "download_ready": False,
                    }

                problem = client.get(f"/api/jobs/{job_id}/problem-tracks").json()
                track = problem["groups"]["manual_review"][0]
                accept_res = client.post(
                    f"/api/jobs/{job_id}/manual-accept",
                    json={
                        "track_id": track["track_id"],
                        "candidate_id": track["best_candidate"]["candidate_id"],
                    },
                )
                rebuild_res = client.post(f"/api/jobs/{job_id}/rebuild-archive")
                payload = rebuild_res.json()
                zip_names = sorted(zipfile.ZipFile(root / payload["download_name"]).namelist())

        self.assertEqual(accept_res.status_code, 200)
        self.assertEqual(rebuild_res.status_code, 200)
        self.assertTrue(payload["download_ready"])
        self.assertEqual(zip_names, ["ok.mp3", "weak.mp3"])

    def test_rejected_track_does_not_get_into_rebuilt_archive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accepted_dir = root / "accepted"
            accepted_dir.mkdir(parents=True)
            (accepted_dir / "ok.mp3").write_bytes(b"ok")

            weak = candidate("Other - Weak", 0.66)
            items = [
                quality.make_quality_item(
                    "Artist - Accepted",
                    quality.STATUS_OK,
                    selected_candidate=candidate("Artist - Accepted", 0.95),
                    file=str(accepted_dir / "ok.mp3"),
                ),
                quality.make_quality_item(
                    "Artist - Weak",
                    quality.STATUS_LOW_CONFIDENCE_MATCH,
                    reason="weak",
                    selected_candidate=weak,
                ),
            ]
            candidates_report = [
                {
                    "input_track": "Artist - Weak",
                    "decision_reason": "http_only_selenium_disabled",
                    "from_cache": False,
                    "provider_results": [],
                    "candidates": [quality.candidate_to_dict(weak)],
                }
            ]
            quality.write_quality_reports(root, items, candidates_report, total=2)

            with TestClient(app) as client:
                job_id = "job_rebuild_reject"
                with app.state.jobs_lock:
                    app.state.jobs[job_id] = {
                        "job_id": job_id,
                        "status": "partial_success",
                        "phase": "done",
                        "saved_run": str(root),
                        "work_root": None,
                        "delivery": "zip",
                        "total_lines": 2,
                        "counts": {"ok": 1, "failed": 1, "skipped": 0},
                        "quality_counts": {},
                        "download_ready": False,
                    }

                problem = client.get(f"/api/jobs/{job_id}/problem-tracks").json()
                track = problem["groups"]["manual_review"][0]
                reject_res = client.post(
                    f"/api/jobs/{job_id}/manual-reject",
                    json={
                        "track_id": track["track_id"],
                        "candidate_id": track["best_candidate"]["candidate_id"],
                        "reason": "wrong version",
                    },
                )
                rebuild_res = client.post(f"/api/jobs/{job_id}/rebuild-archive")
                payload = rebuild_res.json()
                zip_names = zipfile.ZipFile(root / payload["download_name"]).namelist()

        self.assertEqual(reject_res.status_code, 200)
        self.assertEqual(rebuild_res.status_code, 200)
        self.assertEqual(zip_names, ["ok.mp3"])

    def test_retry_not_found_creates_child_job(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            items = [
                quality.make_quality_item("Artist - Missing 1", quality.STATUS_NOT_FOUND, reason="miss"),
                quality.make_quality_item("Artist - Missing 2", quality.STATUS_NOT_FOUND, reason="miss"),
                quality.make_quality_item("Artist - Weak", quality.STATUS_LOW_CONFIDENCE_MATCH, reason="weak"),
            ]
            quality.write_quality_reports(root, items, [], total=3)

            async def fake_run_job(*_args, **_kwargs):
                return None

            with patch("web.app._run_job", fake_run_job):
                with TestClient(app) as client:
                    parent_job_id = "job_retry_parent"
                    with app.state.jobs_lock:
                        app.state.jobs[parent_job_id] = {
                            "job_id": parent_job_id,
                            "status": "partial_success",
                            "phase": "done",
                            "saved_run": str(root),
                            "work_root": None,
                            "delivery": "zip",
                            "run_settings": {"speed_profile": "fast", "parallel_tasks": "auto"},
                            "total_lines": 3,
                            "counts": {"ok": 0, "failed": 3, "skipped": 0},
                            "quality_counts": {},
                        }

                    res = client.post(
                        f"/api/jobs/{parent_job_id}/retry",
                        json={
                            "statuses": ["NOT_FOUND"],
                            "track_ids": [],
                            "search_mode": "deep",
                            "bypass_cache": True,
                            "enable_selenium": True,
                            "provider_timeout_sec": 12,
                            "custom_queries": {},
                        },
                    )

                    self.assertEqual(res.status_code, 202)
                    payload = res.json()
                    self.assertNotEqual(payload["job_id"], parent_job_id)
                    self.assertEqual(payload["parent_job_id"], parent_job_id)
                    self.assertEqual(payload["total_lines"], 2)

                    retry_job = app.state.jobs[payload["job_id"]]
                    self.assertEqual(retry_job["parent_job_id"], parent_job_id)
                    self.assertEqual(retry_job["delivery"], "zip")
                    self.assertEqual(retry_job["run_settings"]["search_mode"], "deep")
                    self.assertTrue(retry_job["run_settings"]["bypass_cache"])
                    self.assertTrue(retry_job["run_settings"]["enable_selenium"])
                    self.assertEqual(retry_job["run_settings"]["provider_timeout_sec"], 12.0)

    def test_retry_not_found_uses_only_not_found_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            items = [
                quality.make_quality_item("Artist - Missing 1", quality.STATUS_NOT_FOUND, reason="miss"),
                quality.make_quality_item("Artist - Missing 2", quality.STATUS_NOT_FOUND, reason="miss"),
                quality.make_quality_item("Artist - Weak", quality.STATUS_LOW_CONFIDENCE_MATCH, reason="weak"),
            ]
            quality.write_quality_reports(root, items, [], total=3)

            async def fake_run_job(*_args, **_kwargs):
                return None

            with patch("web.app._run_job", fake_run_job):
                with TestClient(app) as client:
                    parent_job_id = "job_retry_scope"
                    with app.state.jobs_lock:
                        app.state.jobs[parent_job_id] = {
                            "job_id": parent_job_id,
                            "status": "partial_success",
                            "phase": "done",
                            "saved_run": str(root),
                            "work_root": None,
                            "delivery": "zip",
                            "run_settings": {"speed_profile": "fast", "parallel_tasks": "auto"},
                            "total_lines": 3,
                            "counts": {"ok": 0, "failed": 3, "skipped": 0},
                            "quality_counts": {},
                        }

                    res = client.post(f"/api/jobs/{parent_job_id}/retry", json={"statuses": ["NOT_FOUND"], "track_ids": []})

                    self.assertEqual(res.status_code, 202)
                    retry_job = app.state.jobs[res.json()["job_id"]]
                    self.assertEqual(retry_job["total_lines"], 2)
                    self.assertEqual(retry_job["file_name"], f"retry:{parent_job_id}")

    def test_retry_selected_tracks_uses_only_requested_track_ids(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            items = [
                quality.make_quality_item("Artist - Missing 1", quality.STATUS_NOT_FOUND, reason="miss"),
                quality.make_quality_item("Artist - Missing 2", quality.STATUS_NOT_FOUND, reason="miss"),
            ]
            quality.write_quality_reports(root, items, [], total=2)

            async def fake_run_job(*_args, **_kwargs):
                return None

            with patch("web.app._run_job", fake_run_job):
                with TestClient(app) as client:
                    parent_job_id = "job_retry_selected"
                    with app.state.jobs_lock:
                        app.state.jobs[parent_job_id] = {
                            "job_id": parent_job_id,
                            "status": "partial_success",
                            "phase": "done",
                            "saved_run": str(root),
                            "work_root": None,
                            "delivery": "zip",
                            "run_settings": {"speed_profile": "fast", "parallel_tasks": "auto"},
                            "total_lines": 2,
                            "counts": {"ok": 0, "failed": 2, "skipped": 0},
                            "quality_counts": {},
                        }

                    problem_tracks = client.get(f"/api/jobs/{parent_job_id}/problem-tracks").json()
                    selected_id = problem_tracks["groups"]["not_found"][1]["track_id"]

                    res = client.post(
                        f"/api/jobs/{parent_job_id}/retry",
                        json={"statuses": ["NOT_FOUND"], "track_ids": [selected_id]},
                    )

                    self.assertEqual(res.status_code, 202)
                    retry_job = app.state.jobs[res.json()["job_id"]]
                    self.assertEqual(retry_job["total_lines"], 1)


if __name__ == "__main__":
    unittest.main()
