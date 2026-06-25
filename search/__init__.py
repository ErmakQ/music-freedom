from __future__ import annotations

from .models import ProviderResult, SearchCandidate, SearchDecision, TrackQuery
from .orchestrator import SearchOrchestrator

__all__ = [
    "HitmoHTTPProvider",
    "LMusicHTTPProvider",
    "ProviderResult",
    "SearchCandidate",
    "SearchDecision",
    "SearchOrchestrator",
    "SeleniumFallbackProvider",
    "SkySoundHTTPProvider",
    "TrackQuery",
    "ZvykiHTTPProvider",
]


def __getattr__(name: str):
    if name in {
        "HitmoHTTPProvider",
        "LMusicHTTPProvider",
        "SeleniumFallbackProvider",
        "SkySoundHTTPProvider",
        "ZvykiHTTPProvider",
    }:
        from . import providers

        return getattr(providers, name)
    raise AttributeError(name)
