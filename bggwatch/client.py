"""A deliberately slow HTTP client for boardgamegeek.com.

Guarantees, in order of importance:
  * never more than `request_budget` network requests per run
  * one request at a time, at least `min_delay` seconds apart
  * exponential backoff on 202 / 429 / 5xx, honouring Retry-After
  * the run aborts after `max_failures` failed requests instead of hammering
  * anything fetched within `cache_ttl_hours` is served from disk

The API token is read from the BGG_API_TOKEN environment variable only. It is
never written to disk, logged, or included in cache keys.
"""

from __future__ import annotations

import hashlib
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__
from .config import Config

BASE_URL = "https://boardgamegeek.com"
# Note: BGG's CDN rejects User-Agents containing words like "crawler".
USER_AGENT = f"bgg-trick-watch/{__version__}"
RETRY_STATUSES = {202, 429, 500, 502, 503, 504}


class BudgetExceeded(RuntimeError):
    """The per-run request budget is spent."""


class TooManyFailures(RuntimeError):
    """BGG is unhappy; stop rather than keep retrying."""


class PoliteClient:
    def __init__(self, cfg: Config, *, opener=None, sleep=time.sleep, clock=time.monotonic):
        self.cfg = cfg
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep
        self._clock = clock
        self._last_request = None
        self._token = os.environ.get("BGG_API_TOKEN", "").strip()
        self.requests_made = 0
        self.cache_hits = 0
        self.failures = 0

    @property
    def remaining(self) -> int:
        return self.cfg.request_budget - self.requests_made

    # -- cache ---------------------------------------------------------------
    def _cache_path(self, url: str) -> Path:
        return Path(self.cfg.cache_dir) / (hashlib.sha256(url.encode()).hexdigest() + ".body")

    def _cache_get(self, url: str):
        path = self._cache_path(url)
        try:
            age_hours = (time.time() - path.stat().st_mtime) / 3600
        except OSError:
            return None
        return path.read_bytes() if age_hours < self.cfg.cache_ttl_hours else None

    def _cache_put(self, url: str, body: bytes) -> None:
        path = self._cache_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    # -- network -------------------------------------------------------------
    def _throttle(self) -> None:
        if self._last_request is not None:
            wait = self.cfg.min_delay + random.uniform(0, self.cfg.jitter)
            elapsed = self._clock() - self._last_request
            if elapsed < wait:
                self._sleep(wait - elapsed)

    def _send(self, url: str):
        """One network request. Returns (status, body, retry_after_seconds)."""
        if self.requests_made >= self.cfg.request_budget:
            raise BudgetExceeded(f"request budget of {self.cfg.request_budget} spent")
        self._throttle()
        headers = {"User-Agent": USER_AGENT, "Accept": "application/xml, application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(url, headers=headers)
        self.requests_made += 1
        try:
            with self._open(req, timeout=self.cfg.timeout) as resp:
                return resp.status, resp.read(), None
        except urllib.error.HTTPError as e:
            retry_after = e.headers.get("Retry-After") if e.headers else None
            return e.code, b"", float(retry_after) if (retry_after or "").isdigit() else None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            return 0, b"", None
        finally:
            self._last_request = self._clock()

    def get(self, path: str, params: dict | None = None) -> bytes:
        url = f"{BASE_URL}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        cached = self._cache_get(url)
        if cached is not None:
            self.cache_hits += 1
            return cached

        for attempt in range(self.cfg.max_retries + 1):
            status, body, retry_after = self._send(url)
            if status == 200:
                self._cache_put(url, body)
                return body
            if status not in RETRY_STATUSES and status != 0:
                break  # 4xx: retrying will not help
            if attempt < self.cfg.max_retries:
                self._sleep(max(retry_after or 0, min(120, 5 * 2**attempt)))

        self.failures += 1
        if self.failures >= self.cfg.max_failures:
            raise TooManyFailures(f"{self.failures} failed requests this run; last was HTTP {status}")
        raise RuntimeError(f"GET {path} failed with HTTP {status}")
