from __future__ import annotations

import json
import sqlite3
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

from .models import SearchDecision, TrackQuery


SEARCH_LOGIC_VERSION = "search-v2"


class SearchCache:
    def __init__(
        self,
        path: str | Path,
        ttl_days: int = 14,
        *,
        negative_ttl_hours: int = 12,
        bypass_negative_cache_for_deep: bool = True,
        enabled: bool = True,
    ) -> None:
        self.path = Path(path)
        self.positive_ttl_sec = max(0, int(ttl_days)) * 24 * 60 * 60
        self.negative_ttl_sec = max(0, int(negative_ttl_hours)) * 60 * 60
        self.bypass_negative_cache_for_deep = bypass_negative_cache_for_deep
        self.enabled = enabled
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path))

    def _init_db(self) -> None:
        con = self._connect()
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS search_cache (
                    cache_key TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    status TEXT,
                    reason TEXT,
                    decision_reason TEXT,
                    payload TEXT NOT NULL
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_search_cache_created ON search_cache(created_at)")
            con.commit()
        finally:
            con.close()

    @staticmethod
    def key_for(track: TrackQuery) -> str:
        payload = {
            "artist": " ".join((track.artist or "").strip().lower().split()),
            "title": " ".join((track.title or "").strip().lower().split()),
            "search_mode": track.cache_context.get("search_mode"),
            "enabled_providers": sorted(track.cache_context.get("enabled_providers") or []),
            "enable_selenium_fallback": bool(track.cache_context.get("enable_selenium_fallback")),
            "provider_origins": track.cache_context.get("provider_origins") or {},
            "search_logic_version": track.cache_context.get("search_logic_version") or SEARCH_LOGIC_VERSION,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def hit_type_for_payload(payload: dict[str, Any]) -> str:
        return "negative" if payload.get("status") else "positive"

    def _ttl_sec_for_hit_type(self, hit_type: str) -> int:
        return self.negative_ttl_sec if hit_type == "negative" else self.positive_ttl_sec

    def get_entry(self, track: TrackQuery) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        key = self.key_for(track)
        now = time.time()
        con = self._connect()
        try:
            row = con.execute(
                "SELECT created_at, payload FROM search_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        finally:
            con.close()
        if not row:
            return None
        created_at, payload = row
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None
        hit_type = self.hit_type_for_payload(data)
        if (
            hit_type == "negative"
            and self.bypass_negative_cache_for_deep
            and str(track.cache_context.get("search_mode") or "").lower() == "deep"
        ):
            return None
        ttl_sec = self._ttl_sec_for_hit_type(hit_type)
        if ttl_sec and now - float(created_at) > ttl_sec:
            con = self._connect()
            try:
                con.execute("DELETE FROM search_cache WHERE cache_key = ?", (key,))
                con.commit()
            finally:
                con.close()
            return None
        return {
            "cache_key": key,
            "cache_hit_type": hit_type,
            "payload": data,
        }

    def get(self, track: TrackQuery) -> dict[str, Any] | None:
        entry = self.get_entry(track)
        return entry.get("payload") if entry else None

    def put(self, decision: SearchDecision) -> None:
        if not self.enabled:
            return
        key = self.key_for(decision.track)
        decision.cache_key = key
        decision.cache_hit_type = "negative" if decision.status else "positive"
        payload = decision.to_candidate_report()
        con = self._connect()
        try:
            con.execute(
                """
                INSERT OR REPLACE INTO search_cache
                    (cache_key, created_at, status, reason, decision_reason, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    time.time(),
                    decision.status,
                    decision.reason,
                    decision.decision_reason,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            con.commit()
        finally:
            con.close()
