"""LifeStateAdapter: read-only bridge to Facetide's summary (facetide.summary.v1).

Facetide is the only authority for life reviews, scores and goals. WorkBench reads the summary over
Facetide's authenticated HTTP route (never its SQLite file), keeps the last good response as a
display cache with its fetch time, and shows it only on the owner's dashboard Life view. It is not
exposed through the Host API, Agent tools or work memory. Facetide being down never affects the
other dashboard views.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

CONTRACT = "facetide.summary.v1"


class LifeStateAdapter:
    def __init__(self, *, url: str | None, token: str | None, cache_file: str | Path,
                 timeout: float = 3.0, min_interval: float = 60.0, clock=time.time, opener=None):
        self.url, self.token = url or None, token or None
        self.cache_file, self.timeout, self.min_interval = Path(cache_file), timeout, min_interval
        self._clock, self._open = clock, opener or urllib.request.urlopen
        self._lock = threading.Lock()
        self._last_try = 0.0
        self._last: dict | None = None

    @classmethod
    def from_env(cls, database: str | Path) -> "LifeStateAdapter":
        cache = os.environ.get("FACETIDE_CACHE_FILE") or str(Path(database).with_name("facetide-summary-cache.json"))
        return cls(url=os.environ.get("FACETIDE_SUMMARY_URL"), token=os.environ.get("FACETIDE_SUMMARY_TOKEN"),
                   cache_file=cache, timeout=float(os.environ.get("FACETIDE_TIMEOUT_SECONDS", "3")))

    def _cached(self) -> dict | None:
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
            return data if data.get("summary", {}).get("contract") == CONTRACT else None
        except (OSError, ValueError, AttributeError):
            return None

    def _fetch(self) -> dict:
        request = urllib.request.Request(self.url, headers={"Authorization": f"Bearer {self.token}"})
        with self._open(request, timeout=self.timeout) as response:
            body = json.loads(response.read())
        if not isinstance(body, dict) or body.get("contract") != CONTRACT:
            raise ValueError("unexpected contract")
        return body

    def summary(self) -> dict:
        """{"status": fresh|stale|unavailable|not_configured, "fetched_at", "summary", "error"}."""
        if not (self.url and self.token):
            return {"status": "not_configured", "fetched_at": None, "summary": None, "error": None}
        with self._lock:
            now = self._clock()
            if self._last is not None and now - self._last_try < self.min_interval:
                return self._last
            self._last_try = now
            try:
                body = self._fetch()
            except (OSError, ValueError, urllib.error.URLError) as exc:
                reason = getattr(exc, "code", None) or type(exc).__name__
                cached = self._cached()
                self._last = ({"status": "stale", "fetched_at": cached["fetched_at"], "summary": cached["summary"],
                               "error": str(reason)} if cached else
                              {"status": "unavailable", "fetched_at": None, "summary": None, "error": str(reason)})
                return self._last
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            try:
                tmp = self.cache_file.with_suffix(".tmp")
                tmp.write_text(json.dumps({"fetched_at": stamp, "summary": body}, ensure_ascii=False), encoding="utf-8")
                tmp.replace(self.cache_file)
            except OSError:
                pass  # display still works from memory; the next success writes the cache
            self._last = {"status": "fresh", "fetched_at": stamp, "summary": body, "error": None}
            return self._last
