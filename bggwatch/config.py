"""All tunables in one place. Override any field with a BGGWATCH_<FIELD> env var."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass
class Config:
    # --- field of interest -------------------------------------------------
    mechanic_id: int = 2009  # BGG "Trick-taking"
    min_players: int = 2  # a game is in scope if its player range overlaps
    max_players: int = 4  # [min_players, max_players]
    min_weight: float = 1.5
    max_weight: float = 2.0
    min_weight_votes: int = 5  # fewer votes than this => "weight unconfirmed"
    recent_years: int = 1  # releases this new get the benefit of the doubt on weight

    # --- crawl size ---------------------------------------------------------
    window_days: int = 7  # play-count window
    watchlist_size: int = 150  # games that get a per-game plays request
    pool_refresh_days: int = 7  # how often the full mechanic listing is re-read

    # --- politeness ---------------------------------------------------------
    request_budget: int = 300  # hard cap on network requests per run
    min_delay: float = 2.5  # seconds between requests
    jitter: float = 1.0  # plus up to this many random seconds
    max_retries: int = 4  # per request, on 202/429/5xx
    max_failures: int = 5  # failed requests per run before aborting
    cache_ttl_hours: float = 20.0
    timeout: float = 30.0

    # --- paths --------------------------------------------------------------
    data_dir: Path = Path("data")
    cache_dir: Path = Path(".cache")

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls()
        for f in fields(cls):
            raw = os.environ.get(f"BGGWATCH_{f.name.upper()}")
            if raw is not None:
                setattr(cfg, f.name, type(getattr(cfg, f.name))(raw))
        return cfg
