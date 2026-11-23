"""All tunables in one place. Override any field with a BGGWATCH_<FIELD> env var."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass
class Config:
    # --- field of interest -------------------------------------------------
    # BGG mechanic names, spelled as BGG spells them. The first is the primary
    # family: it owns WATCHLIST.md and the main watchlist. The others are
    # crawled with whatever request budget is left each day.
    families: tuple = ("Trick-taking", "Ladder Climbing", "Push Your Luck", "Set Collection")
    mechanic_id: int = 2009  # BGG id of the primary family; the others' ids are learned from BGG's own data
    card_category: str = "Card Game"  # games from the other families must carry this BGG category
    min_players: int = 2  # a game is in scope if its player range overlaps
    max_players: int = 4  # [min_players, max_players]
    min_weight: float = 1.5
    max_weight: float = 2.0
    min_weight_votes: int = 5  # fewer votes than this => "weight unconfirmed"
    recent_years: int = 1  # releases this new get the benefit of the doubt on weight

    # --- crawl size ---------------------------------------------------------
    window_days: int = 7  # play-count window
    watchlist_size: int = 150  # primary-family games that get a per-game plays request
    family_watch_size: int = 40  # the same, for each other family
    pool_refresh_days: int = 7  # how often the full mechanic listing is re-read
    pool_pages_per_run: int = 40  # listing pages read per other family per run (a big listing resumes next day)

    # --- politeness ---------------------------------------------------------
    request_budget: int = 450  # hard cap on network requests per run
    min_delay: float = 2.5  # seconds between requests
    jitter: float = 1.0  # plus up to this many random seconds
    max_retries: int = 4  # per request, on 202/429/5xx
    max_failures: int = 5  # failed requests per run before aborting
    cache_ttl_hours: float = 20.0
    timeout: float = 30.0

    # --- design analysis ----------------------------------------------------
    analysis_sources: int = 24  # games fed to the analysis, split evenly across families with data (hot-list games are added)
    analysis_fresh_share: float = 0.25  # share of each family's slots held for games not in last week's sources
    analysis_variants: int = 100
    analysis_keep: int = 10
    analysis_family_cap: int = 3  # most top-ten places one family may take, when there is more than one
    analysis_memory_weeks: int = 6  # earlier analyses whose designs are listed in the brief and checked for repeats
    analysis_max_similarity: float = 0.45  # reject a core twist sharing this share of its words with an earlier one

    # --- paths --------------------------------------------------------------
    data_dir: Path = Path("data")
    cache_dir: Path = Path(".cache")

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls()
        for f in fields(cls):
            raw = os.environ.get(f"BGGWATCH_{f.name.upper()}")
            if raw is not None:
                cur = getattr(cfg, f.name)
                setattr(cfg, f.name, tuple(s.strip() for s in raw.split(",") if s.strip())
                        if isinstance(cur, tuple) else type(cur)(raw))
        return cfg

    def family_of(self, game: dict) -> str:
        """The family a game is filed under: the first configured family among
        its mechanics. Games with no mechanics on record count as primary."""
        mechanics = game.get("mechanics") or []
        return next((f for f in self.families if f in mechanics), self.families[0])
