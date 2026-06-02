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
import json
import logging
import os
import shutil
import tempfile
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware

from download_service import load_download_config_from_env
from downloader_core import run_playlist_download
from playlist_utils import decode_playlist_bytes, effective_max_playlist_lines, normalize_playlist_line
import quality as quality_layer
from playlist_importers import import_playlist as import_playlist_content
from playlist_importers import save_imported_playlist

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
    cap = _REPORT_LIST_CAP
    return {
        "counts": {
            "ok": len(ok),
            "failed": len(failed),
            "skipped": len(skipped),
        },
        "quality_counts": quality_counts,
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
}

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


def _new_job_dict(
    job_id: str,
    file_name: str,
    delivery: str,
    total_lines: int,
    run_settings: dict | None = None,
) -> dict:
    cfg = _apply_run_settings(load_download_config_from_env(), run_settings)
    return {
        "job_id": job_id,
        "status": "queued",
        "phase": "queued",
        "detail": "Задача поставлена в очередь",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "file_name": file_name,
        "delivery": delivery,
        "total_lines": total_lines,
        "current_index": 0,
        "current_track": None,
        "counts": {"ok": 0, "failed": 0, "skipped": 0},
        "quality_counts": _zero_quality_counts(),
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
) -> dict:
    job_id = uuid.uuid4().hex
    job = _new_job_dict(job_id, file_name, delivery, len(clean), run_settings)
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

        if status == "phase1_start":
            phase = "phase1"
            detail = "Фаза 1: поиск ссылок"
        elif status == "phase2_start":
            phase = "phase2"
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
            detail = f"Фаза 2: скачано {counts['ok']} из {total}"
        elif status in quality_layer.QUALITY_STATUSES:
            counts["failed"] = int(counts.get("failed") or 0) + 1
            _bump_quality_count(quality_counts, status)
            detail = f"Quality: {status} ({counts['failed']})"
        elif status == "fail":
            counts["failed"] = int(counts.get("failed") or 0) + 1
            detail = (
                f"{'Фаза 2' if phase == 'phase2' else 'Фаза 1'}: ошибок {counts['failed']}"
            )

        _job_update(
            app,
            job_id,
            phase=phase,
            detail=detail,
            current_index=i,
            total_lines=total,
            current_track=raw or None,
            counts=counts,
            quality_counts=quality_counts,
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
            available_reports=_available_report_names_for_root(saved_dir),
            download_ready=False,
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
            available_reports=_available_report_names_for_root(saved_dir),
            download_ready=True,
            download_path=os.path.join(saved_dir, download_name),
            download_name=download_name,
            cleanup_after_download=False,
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
        available_reports=_available_report_names_for_root(work_root),
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

    preview = [
        {
            "artist": track.artist,
            "title": track.title,
            "duration_ms": track.duration_ms,
            "line": track.to_playlist_line(),
        }
        for track in imported.tracks
    ]
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
        raise HTTPException(404, detail="Р—Р°РґР°С‡Р° РЅРµ РЅР°Р№РґРµРЅР°")
    if report_name not in _REPORT_DOWNLOAD_NAMES:
        raise HTTPException(404, detail="РћС‚С‡РµС‚ РЅРµ РЅР°Р№РґРµРЅ")

    root = str(job.get("saved_run") or job.get("work_root") or "")
    report_path = Path(root) / report_name
    if not root or not report_path.is_file():
        raise HTTPException(404, detail="РћС‚С‡РµС‚ РЅРµРґРѕСЃС‚СѓРїРµРЅ")

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
