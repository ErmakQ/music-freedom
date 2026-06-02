from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .models import SearchDecision, TrackQuery


class SearchCache:
    def __init__(self, path: str | Path, ttl_days: int = 14, enabled: bool = True) -> None:
        self.path = Path(path)
        self.ttl_sec = max(0, int(ttl_days)) * 24 * 60 * 60
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
        return " ".join((track.raw or "").strip().lower().split())

    def get(self, track: TrackQuery) -> dict[str, Any] | None:
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
        if self.ttl_sec and now - float(created_at) > self.ttl_sec:
            con = self._connect()
            try:
                con.execute("DELETE FROM search_cache WHERE cache_key = ?", (key,))
                con.commit()
            finally:
                con.close()
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None

    def put(self, decision: SearchDecision) -> None:
        if not self.enabled:
            return
        payload = decision.to_candidate_report()
        key = self.key_for(decision.track)
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
