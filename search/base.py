from __future__ import annotations

from abc import ABC, abstractmethod

from .models import ProviderResult, TrackQuery


class ProviderBase(ABC):
    name: str

    @abstractmethod
    async def search(self, track: TrackQuery) -> ProviderResult:
        raise NotImplementedError
