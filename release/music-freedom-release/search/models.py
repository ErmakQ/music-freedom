from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import quality as quality_layer


@dataclass(slots=True)
class TrackQuery:
    raw: str
    artist: str
    title: str
    expected_duration_sec: int | None = None
    generated_queries: list[str] = field(default_factory=list)
    requested_flags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SearchCandidate:
    provider: str
    input_track: str
    search_query: str
    source_url: str | None
    download_url: str
    referer: str | None = None
    cookie_header: str = ""
    raw_artist: str | None = None
    raw_title: str | None = None
    raw_display_title: str | None = None
    duration_sec: int | None = None
    score: float = 0.0
    flags: list[str] = field(default_factory=list)
    hard_reject: bool = False
    reject_reason: str | None = None
    decision_reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        return (self.download_url or self.source_url or self.raw_display_title or "").strip().lower()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ProviderResult:
    provider: str
    candidates: list[SearchCandidate] = field(default_factory=list)
    status: str = "OK"
    reason: str | None = None
    from_cache: bool = False


@dataclass(slots=True)
class SearchDecision:
    track: TrackQuery
    candidates: list[Any] = field(default_factory=list)
    selected_candidate: Any | None = None
    status: str | None = None
    reason: str | None = None
    decision_reason: str = ""
    provider_results: list[ProviderResult] = field(default_factory=list)
    from_cache: bool = False

    def to_candidate_report(self) -> dict[str, Any]:
        candidate_dicts: list[dict[str, Any]] = []
        for candidate in self.candidates:
            if hasattr(candidate, "to_dict"):
                data = candidate.to_dict()
            else:
                data = quality_layer.candidate_to_dict(candidate) or {}
            reason = getattr(candidate, "decision_reason", None)
            if reason:
                data["decision_reason"] = reason
            candidate_dicts.append(data)
        return {
            "input_track": self.track.raw,
            "requested_flags": self.track.requested_flags,
            "generated_queries": self.track.generated_queries,
            "decision_reason": self.decision_reason,
            "status": self.status,
            "reason": self.reason,
            "from_cache": self.from_cache,
            "provider_results": [
                {
                    "provider": result.provider,
                    "status": result.status,
                    "reason": result.reason,
                    "from_cache": result.from_cache,
                    "candidate_count": len(result.candidates),
                }
                for result in self.provider_results
            ],
            "candidates": candidate_dicts,
        }
