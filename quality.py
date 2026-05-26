from __future__ import annotations

import csv
import json
import os
import re
import shutil
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


STATUS_OK = "OK"
STATUS_NOT_FOUND = "NOT_FOUND"
STATUS_DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
STATUS_LOW_CONFIDENCE_MATCH = "LOW_CONFIDENCE_MATCH"
STATUS_REJECTED_CLEAN_VERSION = "REJECTED_CLEAN_VERSION"
STATUS_REJECTED_CENSORED_VERSION = "REJECTED_CENSORED_VERSION"
STATUS_REJECTED_RADIO_VERSION = "REJECTED_RADIO_VERSION"
STATUS_REJECTED_EDITED_VERSION = "REJECTED_EDITED_VERSION"
STATUS_REJECTED_SHORT_VERSION = "REJECTED_SHORT_VERSION"
STATUS_REJECTED_REMIX_VERSION = "REJECTED_REMIX_VERSION"
STATUS_REJECTED_LIVE_VERSION = "REJECTED_LIVE_VERSION"
STATUS_REJECTED_KARAOKE_VERSION = "REJECTED_KARAOKE_VERSION"
STATUS_VERSION_MISMATCH = "VERSION_MISMATCH"
STATUS_DURATION_MISMATCH = "DURATION_MISMATCH"
STATUS_SUSPICIOUS_FILE = "SUSPICIOUS_FILE"
STATUS_NEEDS_MANUAL_REVIEW = "NEEDS_MANUAL_REVIEW"

QUALITY_STATUSES = {
    STATUS_OK,
    STATUS_NOT_FOUND,
    STATUS_DOWNLOAD_FAILED,
    STATUS_LOW_CONFIDENCE_MATCH,
    STATUS_REJECTED_CLEAN_VERSION,
    STATUS_REJECTED_CENSORED_VERSION,
    STATUS_REJECTED_RADIO_VERSION,
    STATUS_REJECTED_EDITED_VERSION,
    STATUS_REJECTED_SHORT_VERSION,
    STATUS_REJECTED_REMIX_VERSION,
    STATUS_REJECTED_LIVE_VERSION,
    STATUS_REJECTED_KARAOKE_VERSION,
    STATUS_VERSION_MISMATCH,
    STATUS_DURATION_MISMATCH,
    STATUS_SUSPICIOUS_FILE,
    STATUS_NEEDS_MANUAL_REVIEW,
}

MANUAL_REVIEW_STATUSES = QUALITY_STATUSES - {STATUS_OK}

NEGATIVE_VERSION_FLAGS = {
    "clean",
    "censored",
    "edited",
    "safe",
    "radio",
    "short",
    "preview",
    "snippet",
    "remix",
    "live",
    "karaoke",
    "instrumental",
    "slowed",
    "reverb",
    "nightcore",
    "sped_up",
    "cover",
    "acoustic",
    "demo",
}

POSITIVE_VERSION_FLAGS = {
    "explicit",
    "uncensored",
    "dirty",
    "original",
    "album",
    "full",
}

LOW_CONFIDENCE_THRESHOLD = 0.65
OK_SCORE_THRESHOLD = 0.85


@dataclass
class TrackCandidate:
    provider: str
    input_track: str
    raw_artist: str | None = None
    raw_title: str | None = None
    raw_display_title: str = ""
    normalized_artist: str | None = None
    normalized_title: str | None = None
    normalized_display_title: str = ""
    source_url: str | None = None
    download_url: str | None = None
    referer: str | None = None
    cookie_header: str = ""
    search_query: str | None = None
    duration_sec: int | None = None
    file_size: int | None = None
    bitrate: int | None = None
    score: float = 0.0
    flags: list[str] = field(default_factory=list)
    hard_reject: bool = False
    reject_reason: str | None = None


@dataclass
class MatchScore:
    final_score: float
    artist_score: float
    title_score: float
    duration_score: float | None
    version_score: float
    provider_score: float
    flags: list[str]
    hard_reject: bool
    reason: str | None


@dataclass
class ValidationResult:
    ok: bool
    status: str
    flags: list[str] = field(default_factory=list)
    reason: str | None = None
    duration_sec: int | None = None
    bitrate: int | None = None
    file_size: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2212"), "-")
_QUOTES = dict.fromkeys(map(ord, "\u2018\u2019\u201a\u201b\u2032\u00b4`"), "'")
_DQUOTES = dict.fromkeys(map(ord, "\u201c\u201d\u201e\u201f\u2033"), '"')


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_track_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.translate(_DASHES)
    text = text.translate(_QUOTES)
    text = text.translate(_DQUOTES)
    text = _strip_accents(text)
    text = text.casefold().replace("ё", "е")
    text = re.sub(r"\b(featuring|feat\.?|ft\.?)\b", " feat ", text, flags=re.I)
    text = re.sub(r"\s*&\s*", " and ", text)
    text = re.sub(r"\bи\b", " and ", text)
    text = re.sub(r"[\[\]{}()<>\"'.,:;!?/\\|+_=~]", " ", text)
    text = re.sub(r"[-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _has_phrase(norm_text: str, phrase: str) -> bool:
    phrase_norm = normalize_track_text(phrase)
    return bool(re.search(rf"(?<!\w){re.escape(phrase_norm)}(?!\w)", norm_text))


def _remove_phrases(norm_text: str, phrases: list[str]) -> str:
    out = norm_text
    for phrase in phrases:
        phrase_norm = normalize_track_text(phrase)
        out = re.sub(rf"(?<!\w){re.escape(phrase_norm)}(?!\w)", " ", out)
    return re.sub(r"\s+", " ", out).strip()


def _add_if_any(flags: set[str], norm_text: str, flag: str, phrases: list[str]) -> None:
    if any(_has_phrase(norm_text, phrase) for phrase in phrases):
        flags.add(flag)


def _detect_version_flags(text: str) -> set[str]:
    norm = normalize_track_text(text)
    flags: set[str] = set()

    positive_uncensored = [
        "uncensored",
        "без цензуры",
        "bez cenzury",
        "без ценз",
    ]
    if any(_has_phrase(norm, phrase) for phrase in positive_uncensored):
        flags.add("uncensored")

    negative_scan = _remove_phrases(norm, positive_uncensored)

    _add_if_any(flags, negative_scan, "clean", ["clean", "clean version", "без мата", "bez mata", "чистая версия"])
    _add_if_any(flags, negative_scan, "censored", ["censored", "censored version", "цензура", "цензурная версия"])
    _add_if_any(flags, negative_scan, "edited", ["edited", "edited version"])
    _add_if_any(flags, negative_scan, "safe", ["safe version", "radio safe"])
    _add_if_any(flags, negative_scan, "radio", ["radio edit", "radio version", "radio mix", "radio cut"])
    _add_if_any(flags, negative_scan, "short", ["short", "short version", "short edit", "cut version"])
    _add_if_any(flags, negative_scan, "preview", ["preview"])
    _add_if_any(flags, negative_scan, "snippet", ["snippet"])
    _add_if_any(flags, negative_scan, "remix", ["remix", "club mix", "extended mix", "dance mix"])
    _add_if_any(flags, negative_scan, "live", ["live", "live version", "live at", "live from"])
    _add_if_any(flags, negative_scan, "karaoke", ["karaoke"])
    _add_if_any(flags, negative_scan, "instrumental", ["instrumental"])
    _add_if_any(flags, negative_scan, "slowed", ["slowed", "slowed down"])
    _add_if_any(flags, negative_scan, "reverb", ["reverb"])
    _add_if_any(flags, negative_scan, "nightcore", ["nightcore"])
    _add_if_any(flags, negative_scan, "sped_up", ["sped up", "speed up", "spedup"])
    _add_if_any(flags, negative_scan, "cover", ["cover", "cover version"])
    _add_if_any(flags, negative_scan, "acoustic", ["acoustic", "acoustic version"])
    _add_if_any(flags, negative_scan, "demo", ["demo", "demo version"])

    _add_if_any(flags, norm, "explicit", ["explicit"])
    _add_if_any(flags, norm, "dirty", ["dirty", "dirty version"])
    _add_if_any(flags, norm, "original", ["original", "original version"])
    _add_if_any(flags, norm, "album", ["album version"])
    _add_if_any(flags, norm, "full", ["full", "full version"])

    return flags


def detect_requested_version_flags(input_track: str) -> set[str]:
    return _detect_version_flags(input_track)


def detect_candidate_version_flags(text: str) -> set[str]:
    return _detect_version_flags(text)


def should_hard_reject_candidate(
    input_flags: set[str],
    candidate_flags: set[str],
) -> tuple[bool, str | None]:
    if "clean" in candidate_flags and not (input_flags & {"clean", "censored", "edited", "safe"}):
        return True, STATUS_REJECTED_CLEAN_VERSION
    if "censored" in candidate_flags and not (input_flags & {"clean", "censored", "edited", "safe"}):
        return True, STATUS_REJECTED_CENSORED_VERSION
    if "edited" in candidate_flags and not (input_flags & {"clean", "censored", "edited", "safe"}):
        return True, STATUS_REJECTED_EDITED_VERSION
    if "safe" in candidate_flags and not (input_flags & {"clean", "censored", "edited", "safe"}):
        return True, STATUS_REJECTED_CLEAN_VERSION
    if "radio" in candidate_flags and "radio" not in input_flags:
        return True, STATUS_REJECTED_RADIO_VERSION
    if candidate_flags & {"short", "preview", "snippet"} and not (input_flags & {"short", "preview", "snippet"}):
        return True, STATUS_REJECTED_SHORT_VERSION
    if candidate_flags & {"remix", "slowed", "reverb", "nightcore", "sped_up", "cover", "acoustic", "demo"} and not (
        input_flags & {"remix", "slowed", "reverb", "nightcore", "sped_up", "cover", "acoustic", "demo"}
    ):
        return True, STATUS_REJECTED_REMIX_VERSION
    if "live" in candidate_flags and "live" not in input_flags:
        return True, STATUS_REJECTED_LIVE_VERSION
    if candidate_flags & {"karaoke", "instrumental"} and not (input_flags & {"karaoke", "instrumental"}):
        return True, STATUS_REJECTED_KARAOKE_VERSION
    return False, None


def _parse_artist_title(raw: str) -> tuple[str, str]:
    raw = raw.strip()
    if " - " in raw:
        artist, title = raw.rsplit(" - ", 1)
        return artist.strip(), title.strip()
    if "-" in raw:
        artist, title = raw.rsplit("-", 1)
        return artist.strip(), title.strip()
    return "", raw


def generate_search_queries(input_track: str) -> list[str]:
    raw = re.sub(r"\s+", " ", input_track.strip())
    if not raw:
        return []

    artist, title = _parse_artist_title(raw)
    input_flags = detect_requested_version_flags(raw)
    out: list[str] = []

    def add(q: str) -> None:
        q = re.sub(r"\s+", " ", q.strip())
        if q and q.casefold() not in {x.casefold() for x in out}:
            out.append(q)

    add(raw)
    if artist and title:
        add(f"{artist} {title}")
        add(f"{title} {artist}")
    else:
        add(raw.replace(" - ", " "))

    if not (input_flags & {"clean", "censored", "edited", "safe", "radio"}):
        base = f"{artist} {title}".strip() if artist and title else raw
        for suffix in ("explicit", "uncensored", "original", "full version", "album version"):
            add(f"{base} {suffix}")

    return out[:8]


def _token_set(text: str) -> set[str]:
    return {tok for tok in normalize_track_text(text).split() if len(tok) > 1}


def _ratio(a: str, b: str) -> float:
    a = normalize_track_text(a)
    b = normalize_track_text(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    seq = SequenceMatcher(None, a, b).ratio()
    ta = set(a.split())
    tb = set(b.split())
    overlap = len(ta & tb) / max(1, len(ta | tb))
    return max(seq, overlap)


def _strip_version_markers_for_match(text: str) -> str:
    norm = normalize_track_text(text)
    phrases = [
        "clean version",
        "clean",
        "censored version",
        "censored",
        "edited version",
        "edited",
        "safe version",
        "radio edit",
        "radio version",
        "short version",
        "short edit",
        "cut version",
        "preview",
        "snippet",
        "remix",
        "club mix",
        "extended mix",
        "live version",
        "live",
        "karaoke",
        "instrumental",
        "slowed",
        "reverb",
        "nightcore",
        "sped up",
        "cover version",
        "cover",
        "acoustic version",
        "acoustic",
        "demo version",
        "demo",
        "explicit",
        "uncensored",
        "dirty version",
        "dirty",
        "original version",
        "original",
        "album version",
        "full version",
        "full",
        "без мата",
        "без цензуры",
        "цензурная версия",
        "чистая версия",
    ]
    return _remove_phrases(norm, phrases)


def _provider_score(provider: str) -> float:
    provider = (provider or "").lower()
    if "hitmo:http" in provider:
        return 0.78
    if "hitmo" in provider:
        return 0.74
    if "lmusic" in provider:
        return 0.68
    return 0.60


def score_candidate(input_track: str, candidate: TrackCandidate) -> MatchScore:
    in_artist, in_title = _parse_artist_title(input_track)
    cand_artist = candidate.raw_artist or ""
    cand_title = candidate.raw_title or ""
    if not cand_artist and not cand_title:
        cand_artist, cand_title = _parse_artist_title(candidate.raw_display_title or "")

    candidate_text = " ".join(
        part
        for part in [
            candidate.raw_display_title,
            candidate.raw_artist or "",
            candidate.raw_title or "",
        ]
        if part
    )
    flags = detect_candidate_version_flags(candidate_text)
    input_flags = detect_requested_version_flags(input_track)
    hard_reject, reason = should_hard_reject_candidate(input_flags, flags)

    artist_score = _ratio(in_artist, cand_artist) if in_artist and cand_artist else 0.55
    title_score = _ratio(_strip_version_markers_for_match(in_title), _strip_version_markers_for_match(cand_title or candidate.raw_display_title))

    input_tokens = _token_set(input_track)
    candidate_tokens = _token_set(candidate_text)
    token_score = len(input_tokens & candidate_tokens) / max(1, len(input_tokens))
    title_score = max(title_score, min(1.0, token_score))

    duration_score: float | None = None
    version_score = 0.72
    if flags & POSITIVE_VERSION_FLAGS:
        version_score += min(0.18, 0.06 * len(flags & POSITIVE_VERSION_FLAGS))
    if flags & NEGATIVE_VERSION_FLAGS:
        version_score -= min(0.45, 0.12 * len(flags & NEGATIVE_VERSION_FLAGS))
    if hard_reject:
        version_score = min(version_score, 0.05)
    version_score = max(0.0, min(1.0, version_score))

    provider_score = _provider_score(candidate.provider)
    final = (
        artist_score * 0.24
        + title_score * 0.46
        + version_score * 0.20
        + provider_score * 0.10
    )
    if hard_reject:
        final = min(final, 0.49)
    final = max(0.0, min(1.0, final))

    candidate.normalized_artist = normalize_track_text(cand_artist) if cand_artist else None
    candidate.normalized_title = normalize_track_text(cand_title) if cand_title else None
    candidate.normalized_display_title = normalize_track_text(candidate.raw_display_title)
    candidate.flags = sorted(flags)
    candidate.hard_reject = hard_reject
    candidate.reject_reason = reason
    candidate.score = round(final, 4)

    return MatchScore(
        final_score=round(final, 4),
        artist_score=round(artist_score, 4),
        title_score=round(title_score, 4),
        duration_score=duration_score,
        version_score=round(version_score, 4),
        provider_score=round(provider_score, 4),
        flags=sorted(flags),
        hard_reject=hard_reject,
        reason=reason,
    )


def rank_candidates(input_track: str, candidates: list[TrackCandidate]) -> list[TrackCandidate]:
    for candidate in candidates:
        score_candidate(input_track, candidate)
    return sorted(
        candidates,
        key=lambda c: (
            0 if c.hard_reject else 1,
            c.score,
            _provider_score(c.provider),
            1 if c.raw_artist or c.raw_title else 0,
        ),
        reverse=True,
    )


def select_best_candidate(
    input_track: str,
    candidates: list[TrackCandidate],
    *,
    min_score: float = LOW_CONFIDENCE_THRESHOLD,
) -> tuple[TrackCandidate | None, str | None, list[TrackCandidate]]:
    ranked = rank_candidates(input_track, candidates)
    for candidate in ranked:
        if candidate.hard_reject:
            continue
        if candidate.score < min_score:
            return None, STATUS_LOW_CONFIDENCE_MATCH, ranked
        return candidate, None, ranked
    if ranked:
        return None, ranked[0].reject_reason or STATUS_NEEDS_MANUAL_REVIEW, ranked
    return None, STATUS_NOT_FOUND, ranked


def _read_audio_metadata(path: str) -> tuple[dict[str, Any], int | None, int | None]:
    try:
        from mutagen import File as MutagenFile  # type: ignore
    except Exception:
        return {}, None, None

    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        return {}, None, None
    if audio is None:
        return {}, None, None

    metadata: dict[str, Any] = {}
    try:
        for key, value in (audio.tags or {}).items():
            if isinstance(value, (list, tuple)):
                metadata[str(key)] = [str(v) for v in value]
            else:
                metadata[str(key)] = str(value)
    except Exception:
        metadata = {}

    duration_sec = None
    bitrate = None
    try:
        if getattr(audio, "info", None) is not None:
            length = getattr(audio.info, "length", None)
            br = getattr(audio.info, "bitrate", None)
            duration_sec = int(round(float(length))) if length else None
            bitrate = int(br) if br else None
    except Exception:
        pass
    return metadata, duration_sec, bitrate


def validate_downloaded_file(
    path: str,
    input_track: str,
    selected_candidate: TrackCandidate,
) -> ValidationResult:
    if not path or not os.path.isfile(path):
        return ValidationResult(False, STATUS_DOWNLOAD_FAILED, reason="file_missing")

    try:
        file_size = os.path.getsize(path)
    except OSError as exc:
        return ValidationResult(False, STATUS_SUSPICIOUS_FILE, reason=f"stat_failed: {exc}")

    if file_size <= 0:
        return ValidationResult(False, STATUS_SUSPICIOUS_FILE, reason="empty_file", file_size=file_size)
    if file_size < 16 * 1024:
        return ValidationResult(False, STATUS_SUSPICIOUS_FILE, reason="file_too_small", file_size=file_size)

    metadata, duration_sec, bitrate = _read_audio_metadata(path)
    metadata_text = " ".join(
        " ".join(v) if isinstance(v, list) else str(v)
        for v in metadata.values()
    )
    combined_text = " ".join(
        part
        for part in [
            selected_candidate.raw_display_title,
            selected_candidate.raw_artist or "",
            selected_candidate.raw_title or "",
            metadata_text,
        ]
        if part
    )
    flags = detect_candidate_version_flags(combined_text)
    hard_reject, reason = should_hard_reject_candidate(
        detect_requested_version_flags(input_track),
        flags,
    )
    if hard_reject:
        return ValidationResult(
            False,
            reason or STATUS_NEEDS_MANUAL_REVIEW,
            flags=sorted(flags),
            reason="post_download_version_flags",
            duration_sec=duration_sec,
            bitrate=bitrate,
            file_size=file_size,
            metadata=metadata,
        )

    expected_duration = selected_candidate.duration_sec
    if duration_sec is not None and expected_duration is not None:
        delta = abs(duration_sec - expected_duration)
        if delta > 60:
            return ValidationResult(
                False,
                STATUS_VERSION_MISMATCH,
                flags=sorted(flags),
                reason=f"duration_delta_gt_60s: {delta}",
                duration_sec=duration_sec,
                bitrate=bitrate,
                file_size=file_size,
                metadata=metadata,
            )
        if delta > 20:
            return ValidationResult(
                False,
                STATUS_DURATION_MISMATCH,
                flags=sorted(flags),
                reason=f"duration_delta_gt_20s: {delta}",
                duration_sec=duration_sec,
                bitrate=bitrate,
                file_size=file_size,
                metadata=metadata,
            )

    if duration_sec is not None and duration_sec < 30:
        return ValidationResult(
            False,
            STATUS_SUSPICIOUS_FILE,
            flags=sorted(flags),
            reason="duration_lt_30s",
            duration_sec=duration_sec,
            bitrate=bitrate,
            file_size=file_size,
            metadata=metadata,
        )

    return ValidationResult(
        True,
        STATUS_OK,
        flags=sorted(flags),
        reason=None,
        duration_sec=duration_sec,
        bitrate=bitrate,
        file_size=file_size,
        metadata=metadata,
    )


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for i in range(1, 1000):
        candidate = path.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem}_{os.getpid()}{suffix}")


def move_file_to_dir(path: str, dest_dir: str | Path) -> str:
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    target = _unique_path(dest / Path(path).name)
    shutil.move(path, target)
    return str(target)


def candidate_to_dict(candidate: TrackCandidate | None) -> dict[str, Any] | None:
    if candidate is None:
        return None
    data = asdict(candidate)
    data.pop("cookie_header", None)
    return data


def make_quality_item(
    input_track: str,
    status: str,
    *,
    reason: str | None = None,
    selected_candidate: TrackCandidate | None = None,
    validation: ValidationResult | None = None,
    file: str | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    download = {
        "file": file,
        "size": validation.file_size if validation else None,
        "duration_sec": validation.duration_sec if validation else None,
        "bitrate": validation.bitrate if validation else None,
    }
    return {
        "input_track": input_track,
        "status": status,
        "selected_candidate": candidate_to_dict(selected_candidate),
        "download": download,
        "reason": reason,
        "flags": validation.flags if validation else (selected_candidate.flags if selected_candidate else []),
        "url": url or (selected_candidate.download_url if selected_candidate else None),
    }


def quality_summary(items: list[dict[str, Any]], total: int | None = None) -> dict[str, int]:
    summary = {
        "total": total if total is not None else len(items),
        "ok": 0,
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
    for item in items:
        status = item.get("status")
        if status == STATUS_OK:
            summary["ok"] += 1
        elif status == STATUS_NOT_FOUND:
            summary["not_found"] += 1
        elif status == STATUS_DOWNLOAD_FAILED:
            summary["download_failed"] += 1
        elif status == STATUS_LOW_CONFIDENCE_MATCH:
            summary["low_confidence"] += 1
        elif status in {STATUS_REJECTED_CLEAN_VERSION, STATUS_REJECTED_CENSORED_VERSION, STATUS_REJECTED_EDITED_VERSION}:
            summary["rejected_clean_or_censored"] += 1
        elif status == STATUS_REJECTED_RADIO_VERSION:
            summary["rejected_radio"] += 1
        elif status == STATUS_VERSION_MISMATCH:
            summary["version_mismatch"] += 1
        elif status == STATUS_DURATION_MISMATCH:
            summary["duration_mismatch"] += 1
        elif status == STATUS_SUSPICIOUS_FILE:
            summary["suspicious"] += 1
        if status in MANUAL_REVIEW_STATUSES:
            summary["manual_review"] += 1
    return summary


def quality_counts_for_api(items: list[dict[str, Any]], total: int | None = None) -> dict[str, int]:
    summary = quality_summary(items, total=total)
    return {
        "downloaded_ok": summary["ok"],
        "not_found": summary["not_found"],
        "download_failed": summary["download_failed"],
        "low_confidence": summary["low_confidence"],
        "rejected_clean_or_censored": summary["rejected_clean_or_censored"],
        "rejected_radio": summary["rejected_radio"],
        "version_mismatch": summary["version_mismatch"],
        "duration_mismatch": summary["duration_mismatch"],
        "suspicious": summary["suspicious"],
        "manual_review": summary["manual_review"],
    }


def write_quality_reports(
    run_root: str | Path,
    items: list[dict[str, Any]],
    candidates_by_track: list[dict[str, Any]],
    *,
    total: int | None = None,
) -> dict[str, str]:
    root = Path(run_root)
    root.mkdir(parents=True, exist_ok=True)

    report = {
        "summary": quality_summary(items, total=total),
        "items": items,
    }
    quality_json = root / "quality_report.json"
    quality_csv = root / "quality_report.csv"
    manual_csv = root / "manual_review.csv"
    candidates_json = root / "candidates.json"

    quality_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    candidates_json.write_text(
        json.dumps(candidates_by_track, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with quality_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "input_track",
                "status",
                "reason",
                "selected_provider",
                "selected_title",
                "score",
                "flags",
                "file",
                "duration_sec",
                "file_size",
                "url",
            ],
        )
        writer.writeheader()
        for item in items:
            cand = item.get("selected_candidate") or {}
            dl = item.get("download") or {}
            writer.writerow(
                {
                    "input_track": item.get("input_track"),
                    "status": item.get("status"),
                    "reason": item.get("reason"),
                    "selected_provider": cand.get("provider"),
                    "selected_title": cand.get("raw_display_title") or cand.get("raw_title"),
                    "score": cand.get("score"),
                    "flags": ",".join(item.get("flags") or cand.get("flags") or []),
                    "file": dl.get("file"),
                    "duration_sec": dl.get("duration_sec"),
                    "file_size": dl.get("size"),
                    "url": item.get("url") or cand.get("source_url"),
                }
            )

    with manual_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "input_track",
                "status",
                "reason",
                "best_candidate",
                "provider",
                "score",
                "flags",
                "hard_reject",
                "url",
                "download_url",
                "file",
                "comment",
            ],
        )
        writer.writeheader()
        for item in items:
            if item.get("status") not in MANUAL_REVIEW_STATUSES:
                continue
            cand = item.get("selected_candidate") or {}
            dl = item.get("download") or {}
            writer.writerow(
                {
                    "input_track": item.get("input_track"),
                    "status": item.get("status"),
                    "reason": item.get("reason"),
                    "best_candidate": cand.get("raw_display_title") or cand.get("raw_title"),
                    "provider": cand.get("provider"),
                    "score": cand.get("score"),
                    "flags": ",".join(item.get("flags") or cand.get("flags") or []),
                    "hard_reject": cand.get("hard_reject"),
                    "url": cand.get("source_url"),
                    "download_url": cand.get("download_url"),
                    "file": dl.get("file"),
                    "comment": "",
                }
            )

    return {
        "quality_report_json": str(quality_json),
        "quality_report_csv": str(quality_csv),
        "manual_review_csv": str(manual_csv),
        "candidates_json": str(candidates_json),
    }
