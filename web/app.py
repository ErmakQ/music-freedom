"""
HTTP API: .txt плейлист → скачивание → архив.

Сессия: настройки доставки в localStorage на клиенте; опционально подписанная
cookie с anon_id при WEB_SESSION_SECRET.

Запуск: uvicorn web.app:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import urllib.parse
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware

from download_service import load_download_config_from_env
from downloader_core import DownloadConfig, _download_quality_candidate, parse_artist_title, run_playlist_download
from playlist_utils import decode_playlist_bytes, effective_max_playlist_lines, normalize_playlist_line
import quality as quality_layer
from playlist_importers import import_playlist as import_playlist_content
from playlist_importers import save_imported_playlist
from playlist_importers.preview_diagnostics import analyze_preview_tracks

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s", level=logging.INFO
)
log = logging.getLogger("playlist_web")

MAX_CONCURRENT_JOBS = max(1, min(32, int(os.getenv("MAX_CONCURRENT_JOBS", "10"))))
_DEFAULT_CORS = "http://localhost:5173,http://127.0.0.1:5173"
# Сколько строк списков failed/skipped класть в JSON (заголовок + фронт)
_REPORT_LIST_CAP = max(10, min(500, int(os.getenv("WEB_REPORT_LIST_CAP", "200"))))
_WEB_SESSION_SECRET = os.getenv("WEB_SESSION_SECRET", "").strip()
WEB_SESSION_ENABLED = bool(_WEB_SESSION_SECRET)
_MAX_UPLOAD_BYTES = max(0, int(os.getenv("WEB_MAX_UPLOAD_BYTES", "524288")))  # 512 KiB
_MAX_IMPORT_UPLOAD_BYTES = max(0, int(os.getenv("WEB_IMPORT_MAX_UPLOAD_BYTES", "10485760")))  # 10 MiB
_WEB_FAILED_RUNS_DIR = _PROJECT_ROOT / "downloaded_music" / "web_runs"
_WEB_IMPORTS_DIR = _PROJECT_ROOT / "downloaded_music" / "imports"

_SPEED_PRESETS = {
    "fast": {
        "download_parallel_workers": 10,
        "resolve_parallel_workers": 8,
        "delay_sec": 0.1,
        "search_load_rounds": 1,
        "wait_sec": 6,
        "http_search_timeout": 8.0,
        "enable_selenium_fallback": False,
        "search_mode": "fast",
    },
    "balanced": {
        "download_parallel_workers": 8,
        "resolve_parallel_workers": 6,
        "delay_sec": 0.2,
        "search_load_rounds": 2,
        "wait_sec": 8,
        "http_search_timeout": 8.0,
        "enable_selenium_fallback": False,
        "search_mode": "balanced",
    },
    "deep": {
        "download_parallel_workers": 6,
        "resolve_parallel_workers": 1,
        "delay_sec": 0.5,
        "search_load_rounds": 3,
        "wait_sec": 18,
        "http_search_timeout": 12.0,
        "enable_selenium_fallback": True,
        "search_mode": "deep",
    },
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.download_sem = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
    app.state.executor = ThreadPoolExecutor(
        max_workers=MAX_CONCURRENT_JOBS,
        thread_name_prefix="web_playlist_dl",
    )
    app.state.jobs = {}
    app.state.jobs_lock = threading.Lock()
    log.info("Web API: слотов загрузки=%s", MAX_CONCURRENT_JOBS)
    yield
    ex: ThreadPoolExecutor = app.state.executor
    ex.shutdown(wait=True)
    log.info("Web API: executor остановлен")


app = FastAPI(title="Playlist downloader API", lifespan=lifespan)

_cors = os.getenv("WEB_CORS_ORIGINS", _DEFAULT_CORS).strip()
_origins = [o.strip() for o in _cors.split(",") if o.strip()]
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Playlist-Report"],
    )

if WEB_SESSION_ENABLED:
    _https_only = os.getenv("WEB_SESSION_HTTPS_ONLY", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=_WEB_SESSION_SECRET,
        session_cookie="playlist_sid",
        max_age=60 * 60 * 24 * 90,
        same_site="lax",
        https_only=_https_only,
    )
    log.info("Web API: cookie-сессии включены (anon_id в логах)")
else:
    log.info(
        "Web API: WEB_SESSION_SECRET не задан — без серверной сессии "
        "(настройки формата только в браузере, см. фронт localStorage)"
    )


def _touch_session(request: Request) -> str | None:
    """Стабильный анонимный id в подписанной cookie (как слабый аналог telegram user_id)."""
    if not WEB_SESSION_ENABLED:
        return None
    request.session.setdefault("anon_id", str(uuid.uuid4()))
    return str(request.session["anon_id"])


def _runtime_config_payload(cfg=None, speed_profile: str | None = None) -> dict:
    cfg = cfg or load_download_config_from_env()
    return {
        "speed_profile": speed_profile or "env",
        "download_parallel_workers": cfg.parallel_download_workers,
        "resolve_parallel_workers": cfg.resolve_parallel_workers,
        "delay_sec": cfg.delay_sec,
        "wait_sec": cfg.wait_sec,
        "search_load_rounds": cfg.search_load_rounds,
        "http_search_timeout": cfg.http_search_timeout,
        "provider_timeout_sec": cfg.provider_timeout_sec,
        "http_search_concurrency": cfg.http_search_concurrency,
        "download_concurrency": cfg.download_concurrency,
        "validation_concurrency": cfg.validation_parallel_workers,
        "selenium_max_workers": cfg.selenium_max_workers,
        "enable_selenium_fallback": cfg.enable_selenium_fallback,
        "search_cache_enabled": cfg.search_cache_enabled,
        "search_cache_ttl_days": cfg.search_cache_ttl_days,
        "search_mode": cfg.search_mode,
        "hitmo_fetch_track_pages": cfg.fetch_track_pages_when_search_has_mp3,
        "search_try_http_first": cfg.search_try_http_first,
        "lmusic_fallback_enabled": cfg.lmusic_fallback_enabled,
        "zvyki_fallback_enabled": cfg.zvyki_fallback_enabled,
    }


def _build_run_settings(speed_profile: str, parallel_tasks: str | None) -> dict:
    profile = (speed_profile or "env").strip().lower()
    if profile == "safe":
        profile = "deep"
    if profile not in ("env", "fast", "balanced", "deep"):
        profile = "env"
    settings = {"speed_profile": profile, "parallel_tasks": (parallel_tasks or "auto").strip().lower()}
    return settings


def _normalize_retry_run_settings(
    base_run_settings: dict | None,
    payload: dict | None,
) -> dict:
    run_settings = dict(base_run_settings or {"speed_profile": "env", "parallel_tasks": "auto"})
    payload = payload or {}

    search_mode = str(payload.get("search_mode") or "").strip().lower()
    if search_mode in {"fast", "balanced", "deep"}:
        run_settings["search_mode"] = search_mode

    if "bypass_cache" in payload:
        run_settings["bypass_cache"] = bool(payload.get("bypass_cache"))

    if "enable_selenium" in payload:
        run_settings["enable_selenium"] = bool(payload.get("enable_selenium"))

    provider_timeout = payload.get("provider_timeout_sec")
    if provider_timeout not in (None, ""):
        try:
            run_settings["provider_timeout_sec"] = float(provider_timeout)
        except (TypeError, ValueError):
            pass

    custom_queries = payload.get("custom_queries")
    if isinstance(custom_queries, dict):
        run_settings["custom_queries"] = custom_queries

    return run_settings


def _apply_run_settings(cfg, run_settings: dict | None):
    if not run_settings:
        return cfg
    profile = str(run_settings.get("speed_profile") or "env").lower()
    preset = _SPEED_PRESETS.get(profile)
    if preset:
        cfg.parallel_download_workers = int(preset["download_parallel_workers"])
        cfg.resolve_parallel_workers = int(preset["resolve_parallel_workers"])
        cfg.delay_sec = float(preset["delay_sec"])
        cfg.search_load_rounds = int(preset["search_load_rounds"])
        cfg.wait_sec = int(preset["wait_sec"])
        cfg.http_search_timeout = float(preset["http_search_timeout"])
        cfg.provider_timeout_sec = float(preset["http_search_timeout"])
        cfg.enable_selenium_fallback = bool(preset["enable_selenium_fallback"])
        cfg.search_mode = str(preset["search_mode"])
        cfg.fetch_track_pages_when_search_has_mp3 = False
        cfg.search_try_http_first = True
    parallel = str(run_settings.get("parallel_tasks") or "auto").lower()
    if parallel in {"3", "5", "10"}:
        cfg.download_concurrency = max(1, min(16, int(parallel)))
        cfg.parallel_download_workers = cfg.download_concurrency
    search_mode = str(run_settings.get("search_mode") or "").lower()
    if search_mode in {"fast", "balanced", "deep"}:
        cfg.search_mode = search_mode
    if bool(run_settings.get("bypass_cache")):
        cfg.search_cache_enabled = False
    if "enable_selenium" in run_settings:
        cfg.enable_selenium_fallback = bool(run_settings.get("enable_selenium"))
    provider_timeout = run_settings.get("provider_timeout_sec")
    if provider_timeout not in (None, ""):
        try:
            cfg.provider_timeout_sec = float(provider_timeout)
            cfg.http_search_timeout = float(provider_timeout)
        except (TypeError, ValueError):
            pass
    if cfg.search_mode == "deep":
        cfg.enable_selenium_fallback = True
    return cfg


def _run_download_sync(
    clean: list[str],
    music_dir: str,
    job_log: str,
    on_progress=None,
    run_settings: dict | None = None,
) -> dict:
    cfg = load_download_config_from_env()
    cfg = _apply_run_settings(cfg, run_settings)
    return run_playlist_download(
        clean,
        music_dir,
        log_file=job_log,
        skip_lines=set(),
        on_progress=on_progress,
        cfg=cfg,
    )


def _make_zip(
    ok_paths: list[str],
    work_root: str,
    delivery: str,
) -> tuple[str, str]:
    if delivery == "zip":
        zip_path = os.path.join(work_root, "playlist.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in ok_paths:
                if os.path.isfile(p):
                    zf.write(p, arcname=os.path.basename(p))
        return zip_path, "playlist.zip"
    zip_path = os.path.join(work_root, "tracks.zip")
    with zipfile.ZipFile(zip_path, "w") as zf:
        for p in ok_paths:
            if os.path.isfile(p):
                zf.write(p, arcname=os.path.basename(p), compress_type=zipfile.ZIP_STORED)
    return zip_path, "tracks.zip"


def _cleanup_work_root(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _playlist_report_dict(result: dict) -> dict:
    failed = result.get("failed") or []
    skipped = result.get("skipped") or []
    ok = result.get("ok") or []
    quality_counts = result.get("quality_counts") or {}
    result_summary = _normalized_result_summary(result.get("quality_items") or [], total=len(ok) + len(failed) + len(skipped))
    cap = _REPORT_LIST_CAP
    return {
        "counts": {
            "ok": len(ok),
            "failed": len(failed),
            "skipped": len(skipped),
        },
        "quality_counts": quality_counts,
        "result_summary": result_summary,
        "failed": failed[:cap],
        "failed_truncated": len(failed) > cap,
        "skipped": skipped[:cap],
        "skipped_truncated": len(skipped) > cap,
    }


def _playlist_report_header_value(report: dict) -> str:
    raw = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _job_get(app: FastAPI, job_id: str) -> dict | None:
    with app.state.jobs_lock:
        job = app.state.jobs.get(job_id)
        return copy.deepcopy(job) if job is not None else None


def _job_update(app: FastAPI, job_id: str, **changes) -> None:
    with app.state.jobs_lock:
        job = app.state.jobs.get(job_id)
        if job is None:
            return
        job.update(changes)
        job["updated_at"] = datetime.now().isoformat(timespec="seconds")


def _public_job(job: dict) -> dict:
    return {
        "job_id": job["job_id"],
        "parent_job_id": job.get("parent_job_id"),
        "status": job["status"],
        "phase": job["phase"],
        "detail": job.get("detail"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "file_name": job.get("file_name"),
        "delivery": job.get("delivery"),
        "total_lines": job.get("total_lines"),
        "current_index": job.get("current_index"),
        "current_track": job.get("current_track"),
        "counts": job.get("counts"),
        "quality_counts": job.get("quality_counts"),
        "result_summary": job.get("result_summary"),
        "report": job.get("report"),
        "available_reports": job.get("available_reports") or [],
        "download_ready": job.get("download_ready", False),
        "download_name": job.get("download_name"),
        "saved_run": job.get("saved_run"),
        "runtime_config": job.get("runtime_config"),
    }


def _zero_quality_counts() -> dict:
    return {
        "downloaded_ok": 0,
        "not_found": 0,
        "download_failed": 0,
        "low_confidence": 0,
        "rejected_clean_or_censored": 0,
        "rejected_radio": 0,
        "version_mismatch": 0,
        "duration_mismatch": 0,
        "suspicious": 0,
        "manual_review": 0,
    }


def _zero_result_summary(total: int = 0) -> dict:
    return {
        "total": int(total or 0),
        "accepted": 0,
        "not_found": 0,
        "manual_review": 0,
        "rejected": 0,
        "download_failed": 0,
        "suspicious": 0,
    }


def _status_problem_group(status: str | None) -> str | None:
    status = str(status or "")
    if not status or status in {quality_layer.STATUS_OK, "MANUALLY_ACCEPTED"}:
        return None
    if status == "MANUALLY_REJECTED":
        return "rejected"
    if status == quality_layer.STATUS_NOT_FOUND:
        return "not_found"
    if status == quality_layer.STATUS_DOWNLOAD_FAILED:
        return "download_failed"
    if status == quality_layer.STATUS_SUSPICIOUS_FILE:
        return "suspicious"
    if status.startswith("REJECTED_"):
        return "rejected"
    return "manual_review"


def _user_status_label(status: str | None) -> str:
    status = str(status or "")
    labels = {
        quality_layer.STATUS_OK: "В архиве",
        "MANUALLY_ACCEPTED": "Принято вручную",
        "MANUALLY_REJECTED": "Отклонено вручную",
        quality_layer.STATUS_NOT_FOUND: "Не найдено",
        quality_layer.STATUS_DOWNLOAD_FAILED: "Ошибка скачивания",
        quality_layer.STATUS_LOW_CONFIDENCE_MATCH: "Нужна проверка",
        quality_layer.STATUS_VERSION_MISMATCH: "Нужна проверка",
        quality_layer.STATUS_DURATION_MISMATCH: "Нужна проверка",
        quality_layer.STATUS_SUSPICIOUS_FILE: "Подозрительный файл",
        quality_layer.STATUS_NEEDS_MANUAL_REVIEW: "Нужна проверка",
    }
    if status.startswith("REJECTED_"):
        return "Отклонено фильтром"
    return labels.get(status, "Нужна проверка" if status else "")


def _normalized_result_summary(items: list[dict], total: int | None = None) -> dict:
    summary = _zero_result_summary(total if total is not None else len(items))
    if total is None:
        summary["total"] = len(items)
    for item in items:
        status = str(item.get("status") or "")
        if status == quality_layer.STATUS_OK:
            summary["accepted"] += 1
        if status in quality_layer.MANUAL_REVIEW_STATUSES:
            summary["manual_review"] += 1
        group = _status_problem_group(status)
        if group and group != "manual_review":
            summary[group] += 1
    return summary


def _normalized_result_summary_from_counts(job: dict) -> dict:
    total = int(job.get("total_lines") or 0)
    counts = job.get("counts") or {}
    quality_counts = job.get("quality_counts") or {}
    summary = _zero_result_summary(total)
    summary["accepted"] = int(quality_counts.get("downloaded_ok") or counts.get("ok") or 0)
    summary["not_found"] = int(quality_counts.get("not_found") or 0)
    summary["manual_review"] = int(quality_counts.get("manual_review") or 0)
    summary["rejected"] = (
        int(quality_counts.get("rejected_clean_or_censored") or 0)
        + int(quality_counts.get("rejected_radio") or 0)
    )
    summary["download_failed"] = int(quality_counts.get("download_failed") or 0)
    summary["suspicious"] = int(quality_counts.get("suspicious") or 0)
    return summary


def _bump_quality_count(counts: dict, status: str) -> None:
    if status == quality_layer.STATUS_OK or status == "ok":
        counts["downloaded_ok"] = int(counts.get("downloaded_ok") or 0) + 1
    elif status == quality_layer.STATUS_NOT_FOUND:
        counts["not_found"] = int(counts.get("not_found") or 0) + 1
    elif status == quality_layer.STATUS_DOWNLOAD_FAILED:
        counts["download_failed"] = int(counts.get("download_failed") or 0) + 1
    elif status == quality_layer.STATUS_LOW_CONFIDENCE_MATCH:
        counts["low_confidence"] = int(counts.get("low_confidence") or 0) + 1
    elif status in {
        quality_layer.STATUS_REJECTED_CLEAN_VERSION,
        quality_layer.STATUS_REJECTED_CENSORED_VERSION,
        quality_layer.STATUS_REJECTED_EDITED_VERSION,
    }:
        counts["rejected_clean_or_censored"] = int(counts.get("rejected_clean_or_censored") or 0) + 1
    elif status == quality_layer.STATUS_REJECTED_RADIO_VERSION:
        counts["rejected_radio"] = int(counts.get("rejected_radio") or 0) + 1
    elif status == quality_layer.STATUS_VERSION_MISMATCH:
        counts["version_mismatch"] = int(counts.get("version_mismatch") or 0) + 1
    elif status == quality_layer.STATUS_DURATION_MISMATCH:
        counts["duration_mismatch"] = int(counts.get("duration_mismatch") or 0) + 1
    elif status == quality_layer.STATUS_SUSPICIOUS_FILE:
        counts["suspicious"] = int(counts.get("suspicious") or 0) + 1

    if status in quality_layer.MANUAL_REVIEW_STATUSES:
        counts["manual_review"] = int(counts.get("manual_review") or 0) + 1


_REPORT_DOWNLOAD_NAMES = {
    "quality_report.json": "quality_report.json",
    "quality_report.csv": "quality_report.csv",
    "manual_review.csv": "manual_review.csv",
    "candidates.json": "candidates.json",
    "manual_decisions.json": "manual_decisions.json",
}

_SAFE_ENV_KEYS = (
    "SEARCH_MODE",
    "SEARCH_CACHE_ENABLED",
    "SEARCH_CACHE_TTL_DAYS",
    "NEGATIVE_CACHE_TTL_HOURS",
    "BYPASS_NEGATIVE_CACHE_FOR_DEEP",
    "ENABLE_SELENIUM_FALLBACK",
    "PROVIDER_TIMEOUT_SEC",
    "HTTP_SEARCH_CONCURRENCY",
    "DOWNLOAD_CONCURRENCY",
    "SELENIUM_MAX_WORKERS",
    "LMUSIC_FALLBACK_ENABLED",
    "ZVYKI_FALLBACK_ENABLED",
    "SITE_ORIGIN",
    "LMUSIC_ORIGIN",
    "ZVYKI_ORIGIN",
    "MAX_CONCURRENT_JOBS",
    "WEB_CORS_ORIGINS",
)

_IMPORT_DOWNLOAD_NAMES = {
    "imported_playlist.txt",
    "imported_playlist.json",
    "imported_playlist.csv",
    "import_warnings.txt",
}


def _available_report_names_for_root(root: str | None) -> list[str]:
    if not root:
        return []
    p = Path(root)
    return [name for name in _REPORT_DOWNLOAD_NAMES if (p / name).is_file()]


def _read_json_file(path: Path) -> dict | list | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.warning("Could not read json report: %s", path, exc_info=True)
        return None


def _write_json_file(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _manual_decisions_path(root: str | None) -> Path | None:
    if not root:
        return None
    return Path(root) / "manual_decisions.json"


def _read_manual_decisions(root: str | None) -> dict[str, dict]:
    path = _manual_decisions_path(root)
    if path is None:
        return {}
    payload = _read_json_file(path)
    if isinstance(payload, dict):
        decisions = payload.get("decisions")
        if isinstance(decisions, dict):
            return {
                str(track_id): value
                for track_id, value in decisions.items()
                if isinstance(value, dict)
            }
    return {}


def _write_manual_decisions(root: str | None, decisions: dict[str, dict]) -> None:
    path = _manual_decisions_path(root)
    if path is None:
        return
    _write_json_file(path, {"decisions": decisions})


def _candidate_lookup(candidates_report: list) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for entry in candidates_report:
        if isinstance(entry, dict):
            key = str(entry.get("input_track") or "")
            if key:
                out[key] = entry
    return out


def _track_id(index: int, input_line: str) -> str:
    digest = hashlib.sha1(f"{index}:{input_line}".encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"track_{index}_{digest}"


def _candidate_id(track_id: str, candidate_index: int, candidate: dict) -> str:
    base = str(candidate.get("download_url") or candidate.get("source_url") or candidate.get("raw_display_title") or "")
    digest = hashlib.sha1(f"{track_id}:{candidate_index}:{base}".encode("utf-8", errors="replace")).hexdigest()[:10]
    return f"{track_id}_cand_{candidate_index}_{digest}"


def _candidate_status(candidate: dict) -> str:
    if candidate.get("hard_reject"):
        return str(candidate.get("reject_reason") or "REJECTED")
    score = candidate.get("score")
    try:
        score_value = float(score)
    except (TypeError, ValueError):
        score_value = None
    if score_value is not None and score_value >= quality_layer.LOW_CONFIDENCE_THRESHOLD:
        return "MATCH"
    return "CANDIDATE"


def _quality_candidate_from_dict(candidate: dict, input_track: str) -> quality_layer.TrackCandidate:
    from downloader_core import _display_title_from_download_url, parse_artist_title

    raw_display_title = str(candidate.get("raw_display_title") or candidate.get("raw_title") or "")
    raw_title = candidate.get("raw_title")
    raw_artist = candidate.get("raw_artist")
    download_url = candidate.get("download_url")
    referer = candidate.get("referer")
    if (
        isinstance(download_url, str)
        and "skysound" in str(candidate.get("provider") or "").lower()
        and ("skysound" in raw_display_title.lower() or "skysound" in str(raw_title or "").lower())
    ):
        cleaned_display = _display_title_from_download_url(download_url, referer)
        cleaned_artist, cleaned_title = parse_artist_title(cleaned_display)
        raw_display_title = cleaned_display
        raw_artist = cleaned_artist or raw_artist
        raw_title = cleaned_title or raw_title

    quality_candidate = quality_layer.TrackCandidate(
        provider=str(candidate.get("provider") or "manual"),
        input_track=input_track,
        raw_artist=raw_artist,
        raw_title=raw_title,
        raw_display_title=raw_display_title,
        normalized_artist=quality_layer.normalize_track_text(raw_artist) if raw_artist else candidate.get("normalized_artist"),
        normalized_title=quality_layer.normalize_track_text(raw_title) if raw_title else candidate.get("normalized_title"),
        normalized_display_title=quality_layer.normalize_track_text(raw_display_title or ""),
        source_url=candidate.get("source_url"),
        download_url=download_url,
        referer=referer,
        cookie_header=str(candidate.get("cookie_header") or ""),
        search_query=candidate.get("search_query"),
        duration_sec=candidate.get("duration_sec"),
        file_size=candidate.get("file_size"),
        bitrate=candidate.get("bitrate"),
        score=float(candidate.get("score") or 0.0),
        flags=list(candidate.get("flags") or []),
        hard_reject=bool(candidate.get("hard_reject")),
        reject_reason=candidate.get("reject_reason"),
    )
    decision_reason = candidate.get("decision_reason")
    if decision_reason:
        setattr(quality_candidate, "decision_reason", decision_reason)
    return quality_candidate


_LATIN_RE = re.compile(r"[A-Za-z]")
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")


def _has_latin(text: str) -> bool:
    return bool(_LATIN_RE.search(text or ""))


def _has_cyrillic(text: str) -> bool:
    return bool(_CYRILLIC_RE.search(text or ""))


def _candidate_match_diagnostics(input_line: str, candidate: dict | None) -> list[str]:
    if not isinstance(candidate, dict):
        return []
    notes: list[str] = []
    score = candidate.get("score")
    artist_score = candidate.get("artist_score")
    title_score = candidate.get("title_score")
    version_score = candidate.get("version_score")
    version_flags = [str(flag) for flag in (candidate.get("flags") or []) if str(flag)]
    display_title = str(
        candidate.get("raw_display_title")
        or candidate.get("display_title")
        or candidate.get("raw_title")
        or ""
    )

    if isinstance(artist_score, (int, float)) and isinstance(title_score, (int, float)):
        if artist_score >= 0.8 and title_score < 0.35:
            notes.append("Исполнитель совпал, но название трека не подтвердилось.")
        elif title_score < 0.65:
            notes.append("Название трека совпало неуверенно.")
        if artist_score < 0.65:
            notes.append("Исполнитель совпал неуверенно.")
        if (
            title_score >= 0.75
            and artist_score >= 0.75
            and ((_has_cyrillic(input_line) and _has_latin(display_title)) or (_has_latin(input_line) and _has_cyrillic(display_title)))
        ):
            notes.append("Похоже на корректный транслит или латинскую запись.")

    if version_flags and isinstance(version_score, (int, float)) and version_score < 0.72:
        notes.append(f"У версии есть спорные метки: {', '.join(version_flags[:4])}.")

    if isinstance(score, (int, float)) and score < quality_layer.OK_SCORE_THRESHOLD and not notes:
        notes.append(
            f"Общий score ниже порога автопринятия: {float(score):.2f} < {quality_layer.OK_SCORE_THRESHOLD:.2f}."
        )
    return notes[:3]


def _provider_enabled_map(job: dict | None = None) -> dict[str, bool]:
    runtime = job.get("runtime_config") if isinstance(job, dict) and isinstance(job.get("runtime_config"), dict) else {}
    return {
        "Hitmo": True,
        "LMusic": bool(runtime.get("lmusic_fallback_enabled", True)),
        "SkySound": bool(runtime.get("skysound_fallback_enabled", True)),
        "Zvyki": bool(runtime.get("zvyki_fallback_enabled", True)),
        "Selenium": bool(runtime.get("enable_selenium_fallback", False)),
        "Cache": bool(runtime.get("search_cache_enabled", True)),
    }


def _provider_label(raw_provider: str) -> str:
    provider = str(raw_provider or "").lower()
    if provider.startswith("hitmo:"):
        return "Hitmo"
    if provider.startswith("lmusic:"):
        return "LMusic"
    if provider.startswith("skysound:"):
        return "SkySound"
    if provider.startswith("zvyki:"):
        return "Zvyki"
    if provider.startswith("selenium:"):
        return "Selenium"
    if provider == "cache":
        return "Cache"
    return raw_provider or "Unknown"


def _provider_diagnostics(
    job: dict | None,
    provider_results: list | None,
    *,
    from_cache: bool | None = None,
    cache_hit_type: str | None = None,
) -> list[dict]:
    enabled_map = _provider_enabled_map(job)
    out: dict[str, dict] = {
        label: {
            "provider": label,
            "enabled": enabled,
            "candidate_count": 0,
            "errors": [],
            "error_count": 0,
            "timeout": False,
            "statuses": [],
            "cache_hits": 0,
            "negative_hits": 0,
        }
        for label, enabled in enabled_map.items()
    }
    for result in provider_results or []:
        if not isinstance(result, dict):
            continue
        label = _provider_label(str(result.get("provider") or ""))
        bucket = out.setdefault(
            label,
            {
                "provider": label,
                "enabled": True,
                "candidate_count": 0,
                "errors": [],
                "error_count": 0,
                "timeout": False,
                "statuses": [],
                "cache_hits": 0,
                "negative_hits": 0,
            },
        )
        bucket["candidate_count"] += int(result.get("candidate_count") or 0)
        status = str(result.get("status") or "")
        reason = str(result.get("reason") or "")
        if status:
            bucket["statuses"].append(status)
        lowered = f"{status} {reason}".lower()
        if "timeout" in lowered:
            bucket["timeout"] = True
        if status and status not in {"OK", "DISABLED"} and reason:
            bucket["errors"].append(reason)
        elif reason and any(token in lowered for token in ("error", "exception", "failed", "timeout", "miss")):
            bucket["errors"].append(reason)
    cache_bucket = out.setdefault(
        "Cache",
        {
            "provider": "Cache",
            "enabled": enabled_map.get("Cache", True),
            "candidate_count": 0,
            "errors": [],
            "error_count": 0,
            "timeout": False,
            "statuses": [],
            "cache_hits": 0,
            "negative_hits": 0,
        },
    )
    if from_cache:
        cache_bucket["cache_hits"] = 1
        if str(cache_hit_type or "") == "negative":
            cache_bucket["negative_hits"] = 1
    result_list: list[dict] = []
    for label in ("Hitmo", "LMusic", "SkySound", "Zvyki", "Selenium", "Cache"):
        bucket = out.get(label)
        if not bucket:
            continue
        unique_errors = [item for idx, item in enumerate(bucket["errors"]) if item and item not in bucket["errors"][:idx]]
        unique_statuses = [item for idx, item in enumerate(bucket["statuses"]) if item and item not in bucket["statuses"][:idx]]
        result_list.append(
            {
                **bucket,
                "errors": unique_errors,
                "error_count": len(unique_errors),
                "statuses": unique_statuses,
                "status": unique_statuses[0] if unique_statuses else None,
                "reason": unique_errors[0] if unique_errors else None,
            }
        )
    return result_list


def _normalized_candidate_payload(
    track_id: str,
    candidate: dict | None,
    *,
    input_line: str = "",
    candidate_index: int = 1,
    can_accept: bool = False,
) -> dict | None:
    if not isinstance(candidate, dict):
        return None
    flags = candidate.get("flags")
    return {
        "candidate_id": _candidate_id(track_id, candidate_index, candidate),
        "title": candidate.get("raw_title") or candidate.get("raw_display_title"),
        "artist": candidate.get("raw_artist"),
        "display_title": candidate.get("raw_display_title") or candidate.get("raw_title"),
        "provider": candidate.get("provider"),
        "source": candidate.get("provider"),
        "score": candidate.get("score"),
        "artist_score": candidate.get("artist_score"),
        "title_score": candidate.get("title_score"),
        "duration_score": candidate.get("duration_score"),
        "version_score": candidate.get("version_score"),
        "provider_score": candidate.get("provider_score"),
        "duration_sec": candidate.get("duration_sec"),
        "version_flags": flags if isinstance(flags, list) else [],
        "status": _candidate_status(candidate),
        "reject_reason": candidate.get("reject_reason"),
        "decision_reason": candidate.get("decision_reason"),
        "match_diagnostics": _candidate_match_diagnostics(input_line, candidate),
        "downloaded_path": None,
        "can_preview": False,
        "can_accept": can_accept,
    }


def _best_candidate_payload(track_id: str, item: dict, candidate_entry: dict | None) -> dict | None:
    selected = item.get("selected_candidate") if isinstance(item.get("selected_candidate"), dict) else None
    input_line = str(item.get("input_track") or "")
    normalized_selected = _normalized_candidate_payload(track_id, selected, input_line=input_line, candidate_index=1)
    if normalized_selected:
        return normalized_selected
    if candidate_entry:
        candidates = candidate_entry.get("candidates")
        if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
            return _normalized_candidate_payload(track_id, candidates[0], input_line=input_line, candidate_index=1)
    return None


def _problem_track_candidates(track_id: str, candidate_entry: dict | None) -> list[dict]:
    if not isinstance(candidate_entry, dict):
        return []
    input_line = str(candidate_entry.get("input_track") or "")
    raw_candidates = candidate_entry.get("candidates")
    if not isinstance(raw_candidates, list):
        return []
    out: list[dict] = []
    for idx, candidate in enumerate(raw_candidates, 1):
        payload = _normalized_candidate_payload(track_id, candidate, input_line=input_line, candidate_index=idx)
        if payload:
            out.append(payload)
    return out


def _problem_track_candidates_with_raw(
    track_id: str,
    item: dict | None,
    candidate_entry: dict | None,
) -> list[tuple[dict, dict]]:
    out: list[tuple[dict, dict]] = []
    raw_candidates = candidate_entry.get("candidates") if isinstance(candidate_entry, dict) else None
    if isinstance(raw_candidates, list):
        input_line = str(item.get("input_track") or "") if isinstance(item, dict) else ""
        for idx, candidate in enumerate(raw_candidates, 1):
            if not isinstance(candidate, dict):
                continue
            payload = _normalized_candidate_payload(
                track_id,
                candidate,
                input_line=input_line,
                candidate_index=idx,
                can_accept=True,
            )
            if payload:
                out.append((payload, candidate))
    if out:
        return out
    selected = item.get("selected_candidate") if isinstance(item, dict) and isinstance(item.get("selected_candidate"), dict) else None
    payload = _normalized_candidate_payload(
        track_id,
        selected,
        input_line=str(item.get("input_track") or "") if isinstance(item, dict) else "",
        candidate_index=1,
        can_accept=True,
    )
    if payload and isinstance(selected, dict):
        out.append((payload, selected))
    return out


def _problem_track_payload(index: int, item: dict, candidate_entry: dict | None = None, job: dict | None = None) -> dict:
    input_line = str(item.get("input_track") or "")
    artist, title = parse_artist_title(input_line)
    track_id = _track_id(index, input_line)
    best_candidate = _best_candidate_payload(track_id, item, candidate_entry)
    decision_reason = None
    from_cache = None
    cache_hit_type = None
    skipped_selenium_reason = None
    provider_results = []
    if candidate_entry:
        decision_reason = candidate_entry.get("decision_reason")
        from_cache = candidate_entry.get("from_cache")
        cache_hit_type = candidate_entry.get("cache_hit_type")
        skipped_selenium_reason = candidate_entry.get("skipped_selenium_reason")
        raw_provider_results = candidate_entry.get("provider_results")
        if isinstance(raw_provider_results, list):
            provider_results = raw_provider_results
    if not decision_reason and isinstance(best_candidate, dict):
        decision_reason = best_candidate.get("decision_reason")
    provider_summary = _provider_diagnostics(
        job,
        provider_results,
        from_cache=from_cache,
        cache_hit_type=cache_hit_type,
    )
    return {
        "track_id": track_id,
        "index": index,
        "input_line": input_line,
        "artist": artist or None,
        "title": title or None,
        "status": item.get("status"),
        "user_status_label": _user_status_label(item.get("status")),
        "reason": item.get("reason"),
        "decision_reason": decision_reason,
        "best_candidate": best_candidate,
        "match_diagnostics": best_candidate.get("match_diagnostics") if isinstance(best_candidate, dict) else [],
        "score": best_candidate.get("score") if isinstance(best_candidate, dict) else None,
        "from_cache": from_cache,
        "cache_hit_type": cache_hit_type,
        "skipped_selenium_reason": skipped_selenium_reason,
        "provider_errors": [
            error
            for provider in provider_summary
            for error in (provider.get("errors") or [])
            if isinstance(error, str) and error
        ],
        "provider_summary": provider_summary,
    }


def _run_info_text(job_id: str, job: dict) -> str:
    lines = [
        f"job_id: {job_id}",
        f"status: {job.get('status') or '-'}",
        f"phase: {job.get('phase') or '-'}",
        f"file_name: {job.get('file_name') or '-'}",
        f"delivery: {job.get('delivery') or '-'}",
        f"total_lines: {job.get('total_lines') or 0}",
        f"saved_run: {job.get('saved_run') or '-'}",
        f"work_root: {job.get('work_root') or '-'}",
        f"download_name: {job.get('download_name') or '-'}",
        f"detail: {job.get('detail') or '-'}",
    ]
    runtime = job.get("runtime_config") if isinstance(job.get("runtime_config"), dict) else {}
    if runtime:
        lines.append("runtime_config:")
        for key in sorted(runtime):
            lines.append(f"  {key}: {runtime[key]}")
    return "\n".join(lines) + "\n"


def _safe_env_text(job: dict | None = None) -> str:
    lines = ["safe_environment:"]
    for key in _SAFE_ENV_KEYS:
        value = os.getenv(key)
        if value is not None:
            lines.append(f"{key}={value}")
    runtime = job.get("runtime_config") if isinstance(job, dict) and isinstance(job.get("runtime_config"), dict) else {}
    if runtime:
        lines.append("")
        lines.append("runtime_config:")
        for key in sorted(runtime):
            lines.append(f"{key}={runtime[key]}")
    run_settings = job.get("run_settings") if isinstance(job, dict) and isinstance(job.get("run_settings"), dict) else {}
    if run_settings:
        lines.append("")
        lines.append("run_settings:")
        for key in sorted(run_settings):
            lines.append(f"{key}={run_settings[key]}")
    return "\n".join(lines) + "\n"


def _problem_tracks_json_payload(job_id: str, job: dict) -> dict:
    summary, groups = _problem_tracks_payload_for_job(job)
    return {
        "job_id": job_id,
        "summary": summary,
        "groups": groups,
    }


def _diagnostic_zip_bytes(job_id: str, job: dict) -> bytes:
    root = Path(str(job.get("saved_run") or job.get("work_root") or "")).resolve() if str(job.get("saved_run") or job.get("work_root") or "") else None
    summary, groups = _problem_tracks_payload_for_job(job)
    problem_payload = {
        "job_id": job_id,
        "summary": summary,
        "groups": groups,
    }
    not_found_lines = _not_found_lines(str(root) if root else None, job)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        run_info_path = root / "run_info.txt" if root else None
        if run_info_path and run_info_path.is_file():
            archive.writestr("run_info.txt", run_info_path.read_text(encoding="utf-8"))
        else:
            archive.writestr("run_info.txt", _run_info_text(job_id, job))

        for name in ("quality_report.json", "quality_report.csv", "manual_review.csv", "candidates.json"):
            path = root / name if root else None
            if path and path.is_file():
                archive.writestr(name, path.read_bytes())

        job_log_path = root / "job.log" if root else None
        downloaded_log_path = root / "downloaded.log" if root else None
        if downloaded_log_path and downloaded_log_path.is_file():
            archive.writestr("downloaded.log", downloaded_log_path.read_text(encoding="utf-8"))
        elif job_log_path and job_log_path.is_file():
            archive.writestr("downloaded.log", job_log_path.read_text(encoding="utf-8"))

        archive.writestr("not_found.txt", ("\n".join(not_found_lines) + ("\n" if not_found_lines else "")).encode("utf-8"))
        archive.writestr("problem_tracks.json", json.dumps(problem_payload, ensure_ascii=False, indent=2).encode("utf-8"))
        archive.writestr("env_safe.txt", _safe_env_text(job).encode("utf-8"))
    return buffer.getvalue()


def _empty_problem_groups() -> dict[str, list]:
    return {
        "not_found": [],
        "manual_review": [],
        "rejected": [],
        "download_failed": [],
        "suspicious": [],
    }


def _problem_track_records_from_reports(root: str | None, job: dict | None = None) -> tuple[dict | None, dict[str, list], dict[str, dict]]:
    if not root:
        return None, _empty_problem_groups(), {}
    root_path = Path(root)
    report_data = _read_json_file(root_path / "quality_report.json")
    candidates_data = _read_json_file(root_path / "candidates.json")
    if not isinstance(report_data, dict):
        return None, _empty_problem_groups(), {}
    items = report_data.get("items")
    if not isinstance(items, list):
        return None, _empty_problem_groups(), {}
    candidates_lookup = _candidate_lookup(candidates_data if isinstance(candidates_data, list) else [])
    report_summary = report_data.get("summary") if isinstance(report_data.get("summary"), dict) else {}
    summary = _normalized_result_summary(
        [item for item in items if isinstance(item, dict)],
        total=report_summary.get("total"),
    )
    groups = _empty_problem_groups()
    records: dict[str, dict] = {}
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        group = _status_problem_group(item.get("status"))
        if not group:
            continue
        candidate_entry = candidates_lookup.get(str(item.get("input_track") or ""))
        payload = _problem_track_payload(index, item, candidate_entry, job)
        groups[group].append(payload)
        records[payload["track_id"]] = {
            "group": group,
            "track": payload,
            "item": item,
            "candidate_entry": candidate_entry,
        }
    decisions = _read_manual_decisions(root)
    if decisions:
        summary = dict(summary or _zero_result_summary())
        for track_id, decision in decisions.items():
            record = records.get(track_id)
            if record is None:
                continue
            original_group = str(record.get("group") or "")
            track_payload = record.get("track") if isinstance(record.get("track"), dict) else None
            if track_payload is None:
                continue
            if original_group in groups:
                groups[original_group] = [item for item in groups[original_group] if str(item.get("track_id") or "") != track_id]
            original_status = str(track_payload.get("status") or "")
            if original_status in quality_layer.MANUAL_REVIEW_STATUSES:
                summary["manual_review"] = max(0, int(summary.get("manual_review") or 0) - 1)

            action = str(decision.get("action") or "")
            if action == "accept":
                summary["accepted"] = int(summary.get("accepted") or 0) + 1
                continue

            if action == "reject":
                summary["rejected"] = int(summary.get("rejected") or 0) + 1
                updated_track = dict(track_payload)
                updated_track["status"] = "MANUALLY_REJECTED"
                updated_track["user_status_label"] = "Отклонено вручную"
                updated_track["reason"] = decision.get("reason") or "manual reject"
                updated_track["decision_reason"] = decision.get("reason") or track_payload.get("decision_reason")
                candidate_payload = decision.get("candidate")
                if isinstance(candidate_payload, dict):
                    updated_track["best_candidate"] = candidate_payload
                    updated_track["score"] = candidate_payload.get("score")
                groups["rejected"].append(updated_track)
                record["track"] = updated_track
                record["group"] = "rejected"
                record["decision"] = decision
    return summary, groups, records


def _problem_tracks_from_reports(root: str | None, job: dict | None = None) -> tuple[dict | None, dict[str, list]]:
    summary, groups, _records = _problem_track_records_from_reports(root, job)
    return summary, groups


def _problem_tracks_fallback(job: dict) -> tuple[dict, dict[str, list]]:
    summary = job.get("result_summary")
    if not isinstance(summary, dict):
        summary = _normalized_result_summary_from_counts(job)
    groups = _empty_problem_groups()
    report = job.get("report") if isinstance(job.get("report"), dict) else {}
    failed = report.get("failed") if isinstance(report, dict) else []
    if isinstance(failed, list):
        for index, line in enumerate(failed, 1):
            item = {
                "input_track": str(line),
                "status": quality_layer.STATUS_NEEDS_MANUAL_REVIEW,
                "reason": None,
            }
            groups["manual_review"].append(_problem_track_payload(index, item, None, job))
    return summary, groups


def _not_found_lines(root: str | None, job: dict) -> list[str]:
    _summary, groups = _problem_tracks_from_reports(root, job)
    if groups.get("not_found"):
        return [
            str(item.get("input_line") or "").strip()
            for item in groups["not_found"]
            if str(item.get("input_line") or "").strip()
        ]

    report = job.get("report") if isinstance(job.get("report"), dict) else {}
    failed = report.get("failed") if isinstance(report, dict) else []
    if not isinstance(failed, list):
        return []
    return [str(line).strip() for line in failed if str(line).strip()]


def _problem_tracks_payload_for_job(job: dict) -> tuple[dict, dict[str, list]]:
    root = str(job.get("saved_run") or job.get("work_root") or "")
    summary, groups = _problem_tracks_from_reports(root, job)
    if summary is None:
        summary, groups = _problem_tracks_fallback(job)
    return summary, groups


def _problem_track_record_for_job(job: dict, track_id: str) -> dict | None:
    root = str(job.get("saved_run") or job.get("work_root") or "")
    _summary, _groups, records = _problem_track_records_from_reports(root, job)
    record = records.get(track_id)
    if record is not None:
        return record

    fallback_summary, fallback_groups = _problem_tracks_fallback(job)
    for group_name, items in fallback_groups.items():
        for payload in items:
            if str(payload.get("track_id") or "") == track_id:
                return {
                    "group": group_name,
                    "track": payload,
                    "item": {
                        "input_track": payload.get("input_line"),
                        "status": payload.get("status"),
                        "reason": payload.get("reason"),
                    },
                    "candidate_entry": None,
                }
    return None


def _find_problem_candidate_for_record(record: dict, candidate_id: str) -> tuple[dict, dict] | None:
    track = record.get("track") if isinstance(record.get("track"), dict) else {}
    item = record.get("item") if isinstance(record.get("item"), dict) else {}
    candidates = _problem_track_candidates_with_raw(
        str(track.get("track_id") or ""),
        item,
        record.get("candidate_entry") if isinstance(record.get("candidate_entry"), dict) else None,
    )
    for payload, raw_candidate in candidates:
        if str(payload.get("candidate_id") or "") == candidate_id:
            return payload, raw_candidate
    return None


def _unique_target_path(dest_dir: Path, source_name: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    candidate = dest_dir / source_name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for idx in range(1, 1000):
        alt = dest_dir / f"{stem}_{idx}{suffix}"
        if not alt.exists():
            return alt
    return dest_dir / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"


def _copy_file_to_dir(source_path: str, dest_dir: Path) -> str:
    source = Path(source_path)
    target = _unique_target_path(dest_dir, source.name)
    shutil.copy2(source, target)
    return str(target)


def _accepted_paths_for_root(root: str | None) -> list[str]:
    if not root:
        return []
    accepted_dir = Path(root) / "accepted"
    if not accepted_dir.is_dir():
        return []
    return sorted(str(path) for path in accepted_dir.iterdir() if path.is_file())


def _quality_counts_from_summary(summary: dict) -> dict:
    return {
        "downloaded_ok": int(summary.get("accepted") or 0),
        "not_found": int(summary.get("not_found") or 0),
        "download_failed": int(summary.get("download_failed") or 0),
        "low_confidence": 0,
        "rejected_clean_or_censored": int(summary.get("rejected") or 0),
        "rejected_radio": 0,
        "version_mismatch": 0,
        "duration_mismatch": 0,
        "suspicious": int(summary.get("suspicious") or 0),
        "manual_review": int(summary.get("manual_review") or 0),
    }


def _report_from_problem_groups(summary: dict, groups: dict[str, list]) -> dict:
    failed_lines: list[str] = []
    for group_name in ("not_found", "manual_review", "rejected", "download_failed", "suspicious"):
        for item in groups.get(group_name) or []:
            line = str(item.get("input_line") or "").strip()
            if line:
                failed_lines.append(line)
    return {
        "counts": {
            "ok": int(summary.get("accepted") or 0),
            "failed": len(failed_lines),
            "skipped": 0,
        },
        "quality_counts": _quality_counts_from_summary(summary),
        "result_summary": summary,
        "failed": failed_lines[:_REPORT_LIST_CAP],
        "failed_truncated": len(failed_lines) > _REPORT_LIST_CAP,
        "skipped": [],
        "skipped_truncated": False,
    }


def _refresh_job_from_problem_state(app: FastAPI, job_id: str, *, archive_ready: bool | None = None, detail: str | None = None) -> dict | None:
    job = _job_get(app, job_id)
    if job is None:
        return None
    summary, groups = _problem_tracks_payload_for_job(job)
    report = _report_from_problem_groups(summary, groups)
    root = str(job.get("saved_run") or job.get("work_root") or "")
    accepted_paths = _accepted_paths_for_root(root)
    download_ready = bool(job.get("download_ready")) if archive_ready is None else archive_ready
    counts = report["counts"]
    status = "completed" if summary.get("accepted") == summary.get("total") else "partial_success"
    _job_update(
        app,
        job_id,
        status=status,
        phase="done",
        detail=detail or job.get("detail"),
        counts=counts,
        quality_counts=report.get("quality_counts") or _zero_quality_counts(),
        result_summary=summary,
        report=report,
        available_reports=_available_report_names_for_root(root),
        download_ready=download_ready,
        total_lines=int(summary.get("total") or job.get("total_lines") or len(accepted_paths)),
    )
    return _job_get(app, job_id)


def _retry_candidates_for_statuses(job: dict, statuses: list[str]) -> list[dict]:
    _summary, groups = _problem_tracks_payload_for_job(job)
    out: list[dict] = []
    wanted = {str(status).strip().upper() for status in statuses if str(status).strip()}
    status_groups = {
        quality_layer.STATUS_NOT_FOUND: "not_found",
        quality_layer.STATUS_DOWNLOAD_FAILED: "download_failed",
        quality_layer.STATUS_SUSPICIOUS_FILE: "suspicious",
        quality_layer.STATUS_LOW_CONFIDENCE_MATCH: "manual_review",
        quality_layer.STATUS_NEEDS_MANUAL_REVIEW: "manual_review",
    }
    seen: set[str] = set()
    for status in wanted:
        group_name = status_groups.get(status)
        if not group_name:
            continue
        for item in groups.get(group_name) or []:
            item_status = str(item.get("status") or "").strip().upper()
            track_id = str(item.get("track_id") or "")
            if item_status != status or not track_id or track_id in seen:
                continue
            seen.add(track_id)
            out.append(item)
    out.sort(key=lambda item: int(item.get("index") or 0))
    return out


def _new_job_dict(
    job_id: str,
    file_name: str,
    delivery: str,
    total_lines: int,
    run_settings: dict | None = None,
    parent_job_id: str | None = None,
) -> dict:
    cfg = _apply_run_settings(load_download_config_from_env(), run_settings)
    return {
        "job_id": job_id,
        "parent_job_id": parent_job_id,
        "status": "queued",
        "phase": "queued",
        "detail": "Задача поставлена в очередь",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "file_name": file_name,
        "delivery": delivery,
        "total_lines": total_lines,
        "current_index": 0,
        "phase2_completed": 0,
        "current_track": None,
        "counts": {"ok": 0, "failed": 0, "skipped": 0},
        "quality_counts": _zero_quality_counts(),
        "result_summary": _zero_result_summary(total_lines),
        "report": None,
        "available_reports": [],
        "download_ready": False,
        "download_name": None,
        "download_path": None,
        "cleanup_after_download": False,
        "saved_run": None,
        "work_root": None,
        "run_settings": run_settings or {"speed_profile": "env", "parallel_tasks": "auto"},
        "runtime_config": _runtime_config_payload(
            cfg,
            str((run_settings or {}).get("speed_profile") or "env"),
        ),
    }


def _create_processing_job(
    app: FastAPI,
    clean: list[str],
    file_name: str,
    delivery: str,
    anon_id: str | None,
    run_settings: dict | None = None,
    parent_job_id: str | None = None,
) -> dict:
    job_id = uuid.uuid4().hex
    job = _new_job_dict(job_id, file_name, delivery, len(clean), run_settings, parent_job_id)
    with app.state.jobs_lock:
        app.state.jobs[job_id] = job
    asyncio.create_task(_run_job(app, job_id, clean, file_name, delivery, anon_id, run_settings))
    return job


def _job_progress_callback(app: FastAPI, job_id: str):
    def on_progress(i: int, total: int, raw: str, status: str) -> None:
        job = _job_get(app, job_id)
        if job is None:
            return

        counts = dict(job.get("counts") or {})
        quality_counts = dict(job.get("quality_counts") or _zero_quality_counts())
        phase = job.get("phase") or "queued"
        detail = job.get("detail") or ""
        phase2_completed = int(job.get("phase2_completed") or 0)

        if status == "phase1_start":
            phase = "phase1"
            detail = "Фаза 1: поиск ссылок"
        elif status == "phase2_start":
            phase = "phase2"
            phase2_completed = 0
            detail = "Фаза 2: скачивание файлов"
        elif status in ("work", "resolve"):
            phase = "phase1"
            detail = f"Фаза 1: поиск ссылок {i}/{total}"
        elif status == "skip":
            counts["skipped"] = int(counts.get("skipped") or 0) + 1
            detail = f"Пропуск {i}/{total}"
        elif status == "ok":
            phase = "phase2"
            counts["ok"] = int(counts.get("ok") or 0) + 1
            _bump_quality_count(quality_counts, "ok")
            phase2_completed += 1
            detail = f"Фаза 2: скачано {counts['ok']} из {total}"
        elif status in quality_layer.QUALITY_STATUSES:
            counts["failed"] = int(counts.get("failed") or 0) + 1
            _bump_quality_count(quality_counts, status)
            if phase == "phase2":
                phase2_completed += 1
            detail = f"Quality: {status} ({counts['failed']})"
        elif status == "fail":
            counts["failed"] = int(counts.get("failed") or 0) + 1
            if phase == "phase2":
                phase2_completed += 1
            detail = (
                f"{'Фаза 2' if phase == 'phase2' else 'Фаза 1'}: ошибок {counts['failed']}"
            )

        display_index = phase2_completed if phase == "phase2" else i

        _job_update(
            app,
            job_id,
            phase=phase,
            detail=detail,
            current_index=display_index,
            phase2_completed=phase2_completed,
            total_lines=total,
            current_track=raw or None,
            counts=counts,
            quality_counts=quality_counts,
            result_summary=_normalized_result_summary_from_counts(
                {
                    "total_lines": total,
                    "counts": counts,
                    "quality_counts": quality_counts,
                }
            ),
        )

    return on_progress


async def _run_job(
    app: FastAPI,
    job_id: str,
    clean: list[str],
    file_name: str,
    delivery: str,
    anon_id: str | None,
    run_settings: dict | None = None,
) -> None:
    work_root = tempfile.mkdtemp(prefix="web_playlist_")
    music_dir = os.path.join(work_root, "music")
    job_log = os.path.join(work_root, "job.log")
    started_at = datetime.now()

    _job_update(
        app,
        job_id,
        work_root=work_root,
        status="queued",
        phase="queued",
        detail="Ожидание свободного слота",
        current_index=0,
        total_lines=len(clean),
        current_track=None,
    )

    sem: asyncio.Semaphore = app.state.download_sem
    executor: ThreadPoolExecutor = app.state.executor
    loop = asyncio.get_running_loop()
    progress_cb = _job_progress_callback(app, job_id)

    await sem.acquire()
    try:
        _job_update(app, job_id, status="running", detail="Задача запущена")
        try:
            result = await loop.run_in_executor(
                executor,
                lambda: _run_download_sync(clean, music_dir, job_log, progress_cb, run_settings),
            )
        except Exception as exc:
            log.exception("download failed")
            saved_dir = _persist_failed_web_run(
                work_root,
                started_at=started_at,
                file_name=file_name,
                delivery=delivery,
                clean_count=len(clean),
                anon_id=anon_id,
                status="failed",
                detail=f"download exception: {type(exc).__name__}: {exc}",
            )
            _job_update(
                app,
                job_id,
                status="failed",
                phase="done",
                detail=f"Ошибка скачивания: {exc}",
                saved_run=saved_dir,
                download_ready=False,
                report=None,
                current_track=None,
            )
            return
    finally:
        sem.release()

    ok_paths = [p for _, p in result["ok"] if os.path.isfile(p)]
    report = _playlist_report_dict(result)

    if not ok_paths:
        saved_dir = _persist_failed_web_run(
            work_root,
            started_at=started_at,
            file_name=file_name,
            delivery=delivery,
            clean_count=len(clean),
            anon_id=anon_id,
            status="failed_no_tracks",
            detail="Нет успешно скачанных треков",
            result=result,
        )
        _job_update(
            app,
            job_id,
            status="failed",
            phase="done",
            detail="Нет успешно скачанных треков",
            saved_run=saved_dir,
            report=report,
            counts=report["counts"],
            quality_counts=report.get("quality_counts") or _zero_quality_counts(),
            result_summary=report.get("result_summary") or _normalized_result_summary_from_counts(
                {
                    "total_lines": len(clean),
                    "counts": report["counts"],
                    "quality_counts": report.get("quality_counts") or {},
                }
            ),
            available_reports=_available_report_names_for_root(saved_dir),
            download_ready=False,
            current_track=None,
        )
        return

    zip_path, download_name = _make_zip(ok_paths, work_root, delivery)
    if report["counts"]["failed"] > 0 or report["counts"]["skipped"] > 0:
        saved_dir = _persist_failed_web_run(
            work_root,
            started_at=started_at,
            file_name=file_name,
            delivery=delivery,
            clean_count=len(clean),
            anon_id=anon_id,
            status="partial_success",
            detail="Есть успешно скачанные треки, но прогон завершился не полностью",
            result=result,
        )
        _job_update(
            app,
            job_id,
            status="partial_success",
            phase="done",
            detail="Архив готов, но не все треки удалось обработать",
            saved_run=saved_dir,
            report=report,
            counts=report["counts"],
            quality_counts=report.get("quality_counts") or _zero_quality_counts(),
            result_summary=report.get("result_summary") or _normalized_result_summary_from_counts(
                {
                    "total_lines": len(clean),
                    "counts": report["counts"],
                    "quality_counts": report.get("quality_counts") or {},
                }
            ),
            available_reports=_available_report_names_for_root(saved_dir),
            download_ready=True,
            download_path=os.path.join(saved_dir, download_name),
            download_name=download_name,
            cleanup_after_download=False,
            current_track=None,
        )
        return

    _job_update(
        app,
        job_id,
        status="completed",
        phase="done",
        detail="Архив готов к скачиванию",
        report=report,
        counts=report["counts"],
        quality_counts=report.get("quality_counts") or _zero_quality_counts(),
        result_summary=report.get("result_summary") or _normalized_result_summary_from_counts(
            {
                "total_lines": len(clean),
                "counts": report["counts"],
                "quality_counts": report.get("quality_counts") or {},
            }
        ),
        available_reports=_available_report_names_for_root(work_root),
        current_track=None,
        download_ready=True,
        download_path=zip_path,
        download_name=download_name,
        cleanup_after_download=False,
    )


def _persist_failed_web_run(
    work_root: str,
    *,
    started_at: datetime,
    file_name: str,
    delivery: str,
    clean_count: int,
    anon_id: str | None,
    status: str,
    detail: str,
    result: dict | None = None,
) -> str:
    src = Path(work_root)
    if not src.exists():
        return ""

    _WEB_FAILED_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    dest = _WEB_FAILED_RUNS_DIR / run_id
    shutil.move(str(src), str(dest))

    lines = [
        f"run_id: {run_id}",
        f"started_at: {started_at.isoformat(timespec='seconds')}",
        f"finished_at: {datetime.now().isoformat(timespec='seconds')}",
        f"status: {status}",
        f"file_name: {file_name}",
        f"delivery: {delivery}",
        f"playlist_lines: {clean_count}",
        f"anon_id: {anon_id or '-'}",
        f"detail: {detail}",
        f"saved_dir: {dest}",
        f"log_file: {dest / 'job.log'}",
        f"music_dir: {dest / 'music'}",
    ]
    if result is not None:
        lines.extend(
            [
                f"ok: {len(result.get('ok') or [])}",
                f"failed: {len(result.get('failed') or [])}",
                f"skipped: {len(result.get('skipped') or [])}",
                f"quality_counts: {json.dumps(result.get('quality_counts') or {}, ensure_ascii=False)}",
            ]
        )
        failed = result.get("failed") or []
        failed_details = result.get("failed_details") or {}
        download_failed_details = result.get("download_failed_details") or {}
        if failed:
            lines.append("failed_items:")
            for item in failed:
                detail_line = failed_details.get(item)
                if detail_line:
                    lines.append(f"  - {item} | {detail_line}")
                else:
                    lines.append(f"  - {item}")
        if download_failed_details:
            lines.append("download_failed_items:")
            for item, detail_line in download_failed_details.items():
                lines.append(f"  - {item} | {detail_line}")
    (dest / "run_info.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.warning("Web API: saved failed run to %s", dest)
    return str(dest)


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/limits")
async def limits(request: Request):
    _touch_session(request)
    return {
        "max_playlist_lines": effective_max_playlist_lines(),
        "max_concurrent_jobs": MAX_CONCURRENT_JOBS,
        "session_active": WEB_SESSION_ENABLED,
        "runtime_config": _runtime_config_payload(),
        "speed_presets": _SPEED_PRESETS,
    }


@app.post("/api/import-playlist")
async def import_playlist_endpoint(
    request: Request,
    file: UploadFile | None = File(None),
    content: str | None = Form(None),
    run: bool = Form(False),
    delivery: str = Form("zip"),
    speed_profile: str = Form("env"),
    parallel_tasks: str = Form("auto"),
):
    if delivery not in ("zip", "tracks"):
        raise HTTPException(400, detail="delivery должен быть zip или tracks")

    raw_content = content
    file_name = "imported_playlist.txt"
    if file is not None:
        file_name = file.filename or "playlist.html"
        raw_parts: list[bytes] = []
        total = 0
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            raw_parts.append(chunk)
            total += len(chunk)
            if _MAX_IMPORT_UPLOAD_BYTES and total > _MAX_IMPORT_UPLOAD_BYTES:
                raise HTTPException(413, detail="Слишком большой файл импорта")
        raw = b"".join(raw_parts)
        for enc in ("utf-8-sig", "utf-8", "cp1251", "windows-1251"):
            try:
                raw_content = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if raw_content is None:
            raw_content = raw.decode("utf-8", errors="replace")
    elif raw_content is None:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            raw_content = str(payload.get("content") or "")
            run = bool(payload.get("run", run))
            delivery = str(payload.get("delivery") or delivery)
            speed_profile = str(payload.get("speed_profile") or speed_profile)
            parallel_tasks = str(payload.get("parallel_tasks") or parallel_tasks)
    if raw_content is not None:
        raw_content = raw_content.lstrip("\ufeff")
    if delivery not in ("zip", "tracks"):
        raise HTTPException(400, detail="delivery должен быть zip или tracks")

    if not raw_content or not raw_content.strip():
        raise HTTPException(400, detail="Нет данных для импорта")

    try:
        imported = import_playlist_content(content=raw_content)
        files = save_imported_playlist(imported, _WEB_IMPORTS_DIR / datetime.now().strftime("import_%Y%m%d_%H%M%S_%f"))
    except Exception as exc:
        log.exception("playlist import failed")
        raise HTTPException(400, detail=f"Ошибка импорта плейлиста: {exc}") from exc

    import_id = Path(files["dir"]).name
    clean = imported.to_text_lines()
    cap_lines = effective_max_playlist_lines()
    if cap_lines is not None and len(clean) > cap_lines:
        raise HTTPException(
            400,
            detail=f"Слишком много строк после импорта ({len(clean)}), максимум {cap_lines}",
        )

    preview = analyze_preview_tracks(imported.tracks)
    file_links = {
        "txt": f"/api/imports/{import_id}/files/imported_playlist.txt",
        "json": f"/api/imports/{import_id}/files/imported_playlist.json",
        "csv": f"/api/imports/{import_id}/files/imported_playlist.csv",
        "warnings": f"/api/imports/{import_id}/files/import_warnings.txt",
    }
    response: dict = {
        "ok": len(imported.tracks) > 0,
        "source_service": imported.source_service,
        "title": imported.title,
        "source_url": imported.source_url,
        "owner": imported.owner,
        "kind": imported.kind,
        "profile_name": imported.profile_name,
        "profile_url": imported.profile_url,
        "track_count": len(imported.tracks),
        "warnings": imported.warnings,
        "import_id": import_id,
        "files": file_links,
        "file_paths": files,
        "preview": preview,
    }

    if run:
        if not clean:
            response["ok"] = False
            response["warnings"] = imported.warnings + ["NO_TRACKS_IMPORTED: нечего запускать в обработку."]
        else:
            anon = _touch_session(request)
            job = _create_processing_job(
                request.app,
                clean,
                file_name=f"{import_id}:{file_name}",
                delivery=delivery,
                anon_id=anon,
                run_settings=_build_run_settings(speed_profile, parallel_tasks),
            )
            response["job"] = _public_job(job)
    return JSONResponse(status_code=200, content=response)


@app.get("/api/imports/{import_id}/files/{file_name}")
async def download_import_file(request: Request, import_id: str, file_name: str):
    _touch_session(request)
    if file_name not in _IMPORT_DOWNLOAD_NAMES:
        raise HTTPException(404, detail="Файл импорта не найден")
    root = (_WEB_IMPORTS_DIR / import_id).resolve()
    allowed_root = _WEB_IMPORTS_DIR.resolve()
    if allowed_root not in root.parents and root != allowed_root:
        raise HTTPException(404, detail="Файл импорта не найден")
    path = root / file_name
    if not path.is_file():
        raise HTTPException(404, detail="Файл импорта не найден")
    media_type = "application/json" if file_name.endswith(".json") else "text/plain"
    if file_name.endswith(".csv"):
        media_type = "text/csv"
    return FileResponse(str(path), filename=file_name, media_type=media_type)


@app.post("/api/jobs")
async def create_job(
    request: Request,
    file: UploadFile = File(...),
    delivery: str = Form("zip"),
    speed_profile: str = Form("env"),
    parallel_tasks: str = Form("auto"),
):
    if delivery not in ("zip", "tracks"):
        raise HTTPException(400, detail="delivery должен быть zip или tracks")

    name = (file.filename or "").lower()
    if not name.endswith(".txt"):
        raise HTTPException(400, detail="Нужен файл .txt")

    raw_parts: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        raw_parts.append(chunk)
        total += len(chunk)
        if _MAX_UPLOAD_BYTES and total > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                413,
                detail=(
                    f"Слишком большой файл ({total} байт), лимит {_MAX_UPLOAD_BYTES} "
                    "(WEB_MAX_UPLOAD_BYTES в .env)"
                ),
            )

    lines = decode_playlist_bytes(b"".join(raw_parts))
    clean = [ln.strip().lstrip("\ufeff") for ln in lines if normalize_playlist_line(ln)]
    if not clean:
        raise HTTPException(400, detail="В файле нет непустых строк")

    cap_lines = effective_max_playlist_lines()
    if cap_lines is not None and len(clean) > cap_lines:
        raise HTTPException(
            400,
            detail=(
                f"Слишком много строк ({len(clean)}), максимум {cap_lines} "
                "(MAX_PLAYLIST_LINES в .env)"
            ),
        )

    anon = _touch_session(request)
    job_id = uuid.uuid4().hex
    run_settings = _build_run_settings(speed_profile, parallel_tasks)
    runtime_cfg = _apply_run_settings(load_download_config_from_env(), run_settings)
    job = {
        "job_id": job_id,
        "parent_job_id": None,
        "status": "queued",
        "phase": "queued",
        "detail": "Задача поставлена в очередь",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "file_name": file.filename or "",
        "delivery": delivery,
        "total_lines": len(clean),
        "current_index": 0,
        "current_track": None,
        "counts": {"ok": 0, "failed": 0, "skipped": 0},
        "quality_counts": _zero_quality_counts(),
        "result_summary": _zero_result_summary(len(clean)),
        "report": None,
        "available_reports": [],
        "download_ready": False,
        "download_name": None,
        "download_path": None,
        "cleanup_after_download": False,
        "saved_run": None,
        "work_root": None,
        "run_settings": run_settings,
        "runtime_config": _runtime_config_payload(runtime_cfg, run_settings.get("speed_profile")),
    }
    with request.app.state.jobs_lock:
        request.app.state.jobs[job_id] = job

    asyncio.create_task(
        _run_job(request.app, job_id, clean, file.filename or "", delivery, anon, run_settings)
    )
    return JSONResponse(status_code=202, content=_public_job(job))


@app.get("/api/jobs/{job_id}")
async def get_job(request: Request, job_id: str):
    _touch_session(request)
    job = _job_get(request.app, job_id)
    if job is None:
        raise HTTPException(404, detail="Задача не найдена")
    return _public_job(job)


@app.get("/api/jobs/{job_id}/problem-tracks")
async def get_job_problem_tracks(request: Request, job_id: str):
    _touch_session(request)
    job = _job_get(request.app, job_id)
    if job is None:
        raise HTTPException(404, detail="job not found")

    summary, groups = _problem_tracks_payload_for_job(job)

    return {
        "job_id": job_id,
        "summary": summary,
        "groups": groups,
    }


@app.get("/api/jobs/{job_id}/tracks/{track_id}/candidates")
async def get_job_track_candidates(request: Request, job_id: str, track_id: str):
    _touch_session(request)
    job = _job_get(request.app, job_id)
    if job is None:
        raise HTTPException(404, detail="job not found")

    record = _problem_track_record_for_job(job, track_id)
    if record is None:
        raise HTTPException(404, detail="track not found")

    track = record.get("track") if isinstance(record.get("track"), dict) else {}
    candidates = [
        payload
        for payload, _raw_candidate in _problem_track_candidates_with_raw(
            track_id,
            record.get("item") if isinstance(record.get("item"), dict) else None,
            record.get("candidate_entry") if isinstance(record.get("candidate_entry"), dict) else None,
        )
    ]

    return {
        "job_id": job_id,
        "track_id": track_id,
        "input_line": track.get("input_line"),
        "status": track.get("status"),
        "user_status_label": track.get("user_status_label"),
        "decision_reason": track.get("decision_reason"),
        "from_cache": track.get("from_cache"),
        "cache_hit_type": track.get("cache_hit_type"),
        "skipped_selenium_reason": track.get("skipped_selenium_reason"),
        "provider_errors": track.get("provider_errors") or [],
        "provider_summary": track.get("provider_summary") or [],
        "best_candidate": track.get("best_candidate"),
        "candidates": candidates,
    }


@app.post("/api/jobs/{job_id}/manual-accept")
async def manual_accept_track(request: Request, job_id: str):
    _touch_session(request)
    job = _job_get(request.app, job_id)
    if job is None:
        raise HTTPException(404, detail="job not found")

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        raise HTTPException(400, detail="invalid payload")

    track_id = str(payload.get("track_id") or "").strip()
    candidate_id = str(payload.get("candidate_id") or "").strip()
    if not track_id or not candidate_id:
        raise HTTPException(400, detail="track_id and candidate_id are required")

    record = _problem_track_record_for_job(job, track_id)
    if record is None:
        raise HTTPException(404, detail="track not found")
    if str(record.get("group") or "") != "manual_review":
        raise HTTPException(400, detail="manual accept is only available for manual review tracks")

    found = _find_problem_candidate_for_record(record, candidate_id)
    if found is None:
        raise HTTPException(404, detail="candidate not found")
    candidate_payload, raw_candidate = found

    root = str(job.get("saved_run") or job.get("work_root") or "")
    if not root:
        raise HTTPException(409, detail="job workspace is not available")

    item = record.get("item") if isinstance(record.get("item"), dict) else {}
    accepted_dir = Path(root) / "accepted"
    download_dir = Path(root) / "music"
    download_dir.mkdir(parents=True, exist_ok=True)
    accepted_path: str | None = None

    best_candidate = record.get("track", {}).get("best_candidate") if isinstance(record.get("track"), dict) else None
    existing_download = item.get("download", {}).get("file") if isinstance(item.get("download"), dict) else None
    if (
        isinstance(best_candidate, dict)
        and str(best_candidate.get("candidate_id") or "") == candidate_id
        and isinstance(existing_download, str)
        and os.path.isfile(existing_download)
    ):
        accepted_path = _copy_file_to_dir(existing_download, accepted_dir)
    else:
        cfg = _apply_run_settings(load_download_config_from_env(), job.get("run_settings"))
        input_track = str(record.get("track", {}).get("input_line") or item.get("input_track") or "")
        quality_candidate = _quality_candidate_from_dict(raw_candidate, input_track)
        temp_path, error, used_url = _download_quality_candidate(
            input_track,
            quality_candidate,
            str(download_dir),
            cfg,
            quiet=True,
        )
        if not temp_path:
            raise HTTPException(400, detail=error or "candidate download failed")
        validation = quality_layer.validate_downloaded_file(temp_path, input_track, quality_candidate)
        if validation.status in {quality_layer.STATUS_DOWNLOAD_FAILED, quality_layer.STATUS_SUSPICIOUS_FILE}:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise HTTPException(400, detail=validation.reason or validation.status)
        quality_candidate.file_size = validation.file_size
        quality_candidate.bitrate = validation.bitrate
        quality_candidate.duration_sec = validation.duration_sec or quality_candidate.duration_sec
        accepted_path = quality_layer.move_file_to_dir(temp_path, accepted_dir)
        raw_candidate = quality_layer.candidate_to_dict(quality_candidate) or raw_candidate

    decisions = _read_manual_decisions(root)
    decisions[track_id] = {
        "action": "accept",
        "status": "MANUALLY_ACCEPTED",
        "track_id": track_id,
        "input_line": str(record.get("track", {}).get("input_line") or item.get("input_track") or ""),
        "candidate_id": candidate_id,
        "candidate": _normalized_candidate_payload(track_id, raw_candidate, candidate_index=1),
        "accepted_file": accepted_path,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write_manual_decisions(root, decisions)

    refreshed = _refresh_job_from_problem_state(
        request.app,
        job_id,
        archive_ready=False,
        detail="Ручное принятие сохранено. Пересоберите архив, чтобы обновить выдачу.",
    )
    return _public_job(refreshed or job)


@app.post("/api/jobs/{job_id}/manual-reject")
async def manual_reject_track(request: Request, job_id: str):
    _touch_session(request)
    job = _job_get(request.app, job_id)
    if job is None:
        raise HTTPException(404, detail="job not found")

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        raise HTTPException(400, detail="invalid payload")

    track_id = str(payload.get("track_id") or "").strip()
    candidate_id = str(payload.get("candidate_id") or "").strip()
    reason = str(payload.get("reason") or "").strip() or "manual reject"
    if not track_id or not candidate_id:
        raise HTTPException(400, detail="track_id and candidate_id are required")

    record = _problem_track_record_for_job(job, track_id)
    if record is None:
        raise HTTPException(404, detail="track not found")
    if str(record.get("group") or "") != "manual_review":
        raise HTTPException(400, detail="manual reject is only available for manual review tracks")

    found = _find_problem_candidate_for_record(record, candidate_id)
    if found is None:
        raise HTTPException(404, detail="candidate not found")
    candidate_payload, _raw_candidate = found

    root = str(job.get("saved_run") or job.get("work_root") or "")
    if not root:
        raise HTTPException(409, detail="job workspace is not available")

    decisions = _read_manual_decisions(root)
    decisions[track_id] = {
        "action": "reject",
        "status": "MANUALLY_REJECTED",
        "track_id": track_id,
        "input_line": str(record.get("track", {}).get("input_line") or ""),
        "candidate_id": candidate_id,
        "candidate": candidate_payload,
        "reason": reason,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write_manual_decisions(root, decisions)

    refreshed = _refresh_job_from_problem_state(
        request.app,
        job_id,
        archive_ready=False,
        detail="Ручное отклонение сохранено. Пересоберите архив, если нужно обновить состав.",
    )
    return _public_job(refreshed or job)


@app.post("/api/jobs/{job_id}/rebuild-archive")
async def rebuild_archive(request: Request, job_id: str):
    _touch_session(request)
    job = _job_get(request.app, job_id)
    if job is None:
        raise HTTPException(404, detail="job not found")

    root = str(job.get("saved_run") or job.get("work_root") or "")
    if not root:
        raise HTTPException(409, detail="job workspace is not available")

    accepted_paths = _accepted_paths_for_root(root)
    if not accepted_paths:
        raise HTTPException(400, detail="no accepted files available for archive rebuild")

    delivery = str(job.get("delivery") or "zip")
    zip_path, download_name = _make_zip(accepted_paths, root, delivery)
    refreshed = _refresh_job_from_problem_state(
        request.app,
        job_id,
        archive_ready=True,
        detail="Архив пересобран с учетом ручных решений.",
    )
    _job_update(
        request.app,
        job_id,
        download_path=zip_path,
        download_name=download_name,
        cleanup_after_download=False,
    )
    latest = _job_get(request.app, job_id)
    return _public_job(latest or refreshed or job)


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(request: Request, job_id: str):
    parent_job = _job_get(request.app, job_id)
    if parent_job is None:
        raise HTTPException(404, detail="job not found")

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        raise HTTPException(400, detail="invalid retry payload")

    statuses_raw = payload.get("statuses")
    statuses = [str(item).strip().upper() for item in statuses_raw] if isinstance(statuses_raw, list) else []
    if not statuses:
        raise HTTPException(400, detail="statuses are required")
    supported_statuses = {
        quality_layer.STATUS_NOT_FOUND,
        quality_layer.STATUS_DOWNLOAD_FAILED,
        quality_layer.STATUS_LOW_CONFIDENCE_MATCH,
        quality_layer.STATUS_NEEDS_MANUAL_REVIEW,
        quality_layer.STATUS_SUSPICIOUS_FILE,
    }
    unsupported = [status for status in statuses if status not in supported_statuses]
    if unsupported:
        raise HTTPException(400, detail=f"unsupported retry statuses: {', '.join(sorted(set(unsupported)))}")

    track_ids_raw = payload.get("track_ids")
    track_ids = {str(item).strip() for item in track_ids_raw} if isinstance(track_ids_raw, list) else set()
    candidates = _retry_candidates_for_statuses(parent_job, statuses)
    if track_ids:
        candidates = [item for item in candidates if str(item.get("track_id") or "") in track_ids]

    custom_queries_raw = payload.get("custom_queries")
    custom_queries = {
        str(track_id).strip(): str(query).strip()
        for track_id, query in custom_queries_raw.items()
        if str(track_id).strip() and str(query).strip()
    } if isinstance(custom_queries_raw, dict) else {}

    clean: list[str] = []
    effective_custom_queries: dict[str, str] = {}
    for item in candidates:
        track_id = str(item.get("track_id") or "").strip()
        input_line = str(item.get("input_line") or "").strip()
        if not input_line:
            continue
        query = custom_queries.get(track_id) or input_line
        clean.append(query)
        if track_id and track_id in custom_queries:
            effective_custom_queries[track_id] = custom_queries[track_id]
    if not clean:
        raise HTTPException(400, detail="no tracks matched selected statuses/track_ids")

    anon = _touch_session(request)
    run_settings = _normalize_retry_run_settings(parent_job.get("run_settings"), payload)
    if effective_custom_queries:
        run_settings["custom_queries"] = effective_custom_queries
    else:
        run_settings.pop("custom_queries", None)
    delivery = str(parent_job.get("delivery") or "zip")
    retry_job_dict = _create_processing_job(
        request.app,
        clean,
        file_name=f"retry:{job_id}",
        delivery=delivery if delivery in {"zip", "tracks"} else "zip",
        anon_id=anon,
        run_settings=run_settings,
        parent_job_id=job_id,
    )
    return JSONResponse(status_code=202, content=_public_job(retry_job_dict))


@app.get("/api/jobs/{job_id}/not-found.txt")
async def download_job_not_found_txt(request: Request, job_id: str):
    _touch_session(request)
    job = _job_get(request.app, job_id)
    if job is None:
        raise HTTPException(404, detail="job not found")

    root = str(job.get("saved_run") or job.get("work_root") or "")
    lines = _not_found_lines(root, job)
    body = "\n".join(lines)
    if body:
        body += "\n"
    return PlainTextResponse(
        body,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{job_id}-not-found.txt"'},
    )


@app.get("/api/jobs/{job_id}/diagnostic.zip")
async def download_job_diagnostic_zip(request: Request, job_id: str):
    _touch_session(request)
    job = _job_get(request.app, job_id)
    if job is None:
        raise HTTPException(404, detail="job not found")

    payload = _diagnostic_zip_bytes(job_id, job)
    return Response(
        payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{job_id}-diagnostic.zip"'},
    )


@app.get("/api/jobs/{job_id}/download")
async def download_job_result(request: Request, job_id: str):
    _touch_session(request)
    job = _job_get(request.app, job_id)
    if job is None:
        raise HTTPException(404, detail="Задача не найдена")
    if not job.get("download_ready"):
        raise HTTPException(409, detail="Архив ещё не готов")

    download_path = str(job.get("download_path") or "")
    if not download_path or not os.path.isfile(download_path):
        raise HTTPException(410, detail="Файл архива недоступен")

    report = job.get("report")
    headers = (
        {"X-Playlist-Report": _playlist_report_header_value(report)}
        if isinstance(report, dict)
        else {}
    )
    background = None
    if job.get("cleanup_after_download") and job.get("work_root"):
        background = BackgroundTask(_cleanup_work_root, str(job["work_root"]))
        _job_update(
            request.app,
            job_id,
            cleanup_after_download=False,
            detail="Архив скачан",
        )

    return FileResponse(
        download_path,
        filename=str(job.get("download_name") or "playlist.zip"),
        media_type="application/zip",
        headers=headers,
        background=background,
    )


@app.get("/api/jobs/{job_id}/reports/{report_name}")
async def download_job_report(request: Request, job_id: str, report_name: str):
    _touch_session(request)
    job = _job_get(request.app, job_id)
    if job is None:
        raise HTTPException(404, detail="Задача не найдена")
    if report_name not in _REPORT_DOWNLOAD_NAMES:
        raise HTTPException(404, detail="Отчет не найден")

    root = str(job.get("saved_run") or job.get("work_root") or "")
    report_path = Path(root) / report_name
    if not root or not report_path.is_file():
        raise HTTPException(404, detail="Отчет недоступен")

    media_type = "application/json" if report_name.endswith(".json") else "text/csv"
    return FileResponse(
        str(report_path),
        filename=report_name,
        media_type=media_type,
    )


@app.post("/api/process")
async def process_playlist(
    request: Request,
    file: UploadFile = File(...),
    delivery: str = Form("zip"),
):
    if delivery not in ("zip", "tracks"):
        raise HTTPException(400, detail="delivery должен быть zip или tracks")

    name = (file.filename or "").lower()
    if not name.endswith(".txt"):
        raise HTTPException(400, detail="Нужен файл .txt")

    raw_parts: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        raw_parts.append(chunk)
        total += len(chunk)
        if _MAX_UPLOAD_BYTES and total > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                413,
                detail=(
                    f"Слишком большой файл ({total} байт), лимит {_MAX_UPLOAD_BYTES} "
                    "(WEB_MAX_UPLOAD_BYTES в .env)"
                ),
            )
    raw = b"".join(raw_parts)
    lines = decode_playlist_bytes(raw)
    clean = [ln.strip().lstrip("\ufeff") for ln in lines if normalize_playlist_line(ln)]
    if not clean:
        raise HTTPException(400, detail="В файле нет непустых строк")
    cap_lines = effective_max_playlist_lines()
    if cap_lines is not None and len(clean) > cap_lines:
        raise HTTPException(
            400,
            detail=(
                f"Слишком много строк ({len(clean)}), максимум {cap_lines} "
                "(MAX_PLAYLIST_LINES в .env)"
            ),
        )

    anon = _touch_session(request)
    if anon:
        log.info(
            "web process: anon_id=%s lines=%s delivery=%s file=%r",
            anon,
            len(clean),
            delivery,
            file.filename or "",
        )

    work_root = tempfile.mkdtemp(prefix="web_playlist_")
    music_dir = os.path.join(work_root, "music")
    job_log = os.path.join(work_root, "job.log")
    started_at = datetime.now()

    sem: asyncio.Semaphore = request.app.state.download_sem
    executor: ThreadPoolExecutor = request.app.state.executor
    loop = asyncio.get_running_loop()

    await sem.acquire()
    try:
        try:
            result = await loop.run_in_executor(
                executor,
                lambda: _run_download_sync(clean, music_dir, job_log),
            )
        except Exception as e:
            log.exception("download failed")
            saved_dir = _persist_failed_web_run(
                work_root,
                started_at=started_at,
                file_name=file.filename or "",
                delivery=delivery,
                clean_count=len(clean),
                anon_id=anon,
                status="failed",
                detail=f"download exception: {type(e).__name__}: {e}",
            )
            raise HTTPException(500, detail=f"Ошибка скачивания: {e}") from e
    finally:
        sem.release()

    ok_paths = [p for _, p in result["ok"] if os.path.isfile(p)]
    n_ok = len(result["ok"])
    n_fail = len(result["failed"])
    n_skip = len(result["skipped"])

    report = _playlist_report_dict(result)

    if not ok_paths:
        saved_dir = _persist_failed_web_run(
            work_root,
            started_at=started_at,
            file_name=file.filename or "",
            delivery=delivery,
            clean_count=len(clean),
            anon_id=anon,
            status="failed_no_tracks",
            detail="Нет успешно скачанных треков",
            result=result,
        )
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Нет успешно скачанных треков",
                "saved_run": saved_dir,
                **report,
            },
        )

    zip_path, download_name = _make_zip(ok_paths, work_root, delivery)
    if n_fail > 0 or n_skip > 0:
        saved_dir = _persist_failed_web_run(
            work_root,
            started_at=started_at,
            file_name=file.filename or "",
            delivery=delivery,
            clean_count=len(clean),
            anon_id=anon,
            status="partial_success",
            detail="Есть успешно скачанные треки, но прогон завершился не полностью",
            result=result,
        )
        zip_path = os.path.join(saved_dir, download_name)
        return FileResponse(
            zip_path,
            filename=download_name,
            media_type="application/zip",
            headers={"X-Playlist-Report": _playlist_report_header_value(report)},
        )
    return FileResponse(
        zip_path,
        filename=download_name,
        media_type="application/zip",
        headers={"X-Playlist-Report": _playlist_report_header_value(report)},
        background=BackgroundTask(_cleanup_work_root, work_root),
    )


# После `npm run build` в frontend/ — один процесс отдаёт и API, и UI
_frontend_dist = _PROJECT_ROOT / "frontend" / "dist"
if _frontend_dist.is_dir():
    app.mount(
        "/",
        StaticFiles(directory=str(_frontend_dist), html=True),
        name="spa",
    )
