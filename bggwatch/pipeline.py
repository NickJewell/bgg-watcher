"""The daily run: decide what little needs fetching, fetch it, snapshot it."""

from __future__ import annotations

import datetime as dt
import math

from . import bgg
from .client import BudgetExceeded, PoliteClient, TooManyFailures
from .config import Config
from .store import Store

STAT_KEYS = ("usersrated", "average", "rank", "owned", "wishing", "wanting", "numcomments")


def log(msg: str) -> None:
    print(f"[bggwatch] {msg}", flush=True)


# -- pure decisions (unit tested) ----------------------------------------------
def is_candidate(row: dict, cfg: Config, this_year: int) -> bool:
    """Cheap pre-filter on the mechanic listing, before any per-game request."""
    w = row["weight"] or 0
    if cfg.min_weight <= w <= cfg.max_weight:
        return True
    recent = (row["year"] or 0) >= this_year - cfg.recent_years
    return recent and (w == 0 or cfg.min_weight - 0.5 <= w <= cfg.max_weight + 0.5)


def classify(game: dict, cfg: Config) -> str:
    """'confirmed' | 'unconfirmed' (too few weight votes to trust) | 'out'."""
    if game.get("type") != "boardgame":
        return "out"
    lo, hi = game.get("min_players") or 0, game.get("max_players") or 0
    if hi <= 0 or lo > cfg.max_players or hi < cfg.min_players:
        return "out"
    if (game.get("numweights") or 0) < cfg.min_weight_votes:
        return "unconfirmed"
    return "confirmed" if cfg.min_weight <= game["weight"] <= cfg.max_weight else "out"


def priority(game: dict, hot: dict[int, int], this_year: int) -> float:
    """Who deserves one of the limited per-game plays requests."""
    if game["id"] in hot:
        return math.inf
    age = this_year - (game.get("year") or 0)
    recency = 3 if age <= 1 else 2 if age <= 3 else 1
    return ((game.get("wishing") or 0) + (game.get("owned") or 0) / 4) * recency


def select_watchlist(games: dict[int, dict], hot: dict[int, int], cfg: Config, this_year: int) -> list[int]:
    scoped = [g for g in games.values() if g.get("status") in ("confirmed", "unconfirmed")]
    scoped.sort(key=lambda g: (-priority(g, hot, this_year), g["id"]))
    return [g["id"] for g in scoped[: cfg.watchlist_size]]


def chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


# -- steps ---------------------------------------------------------------------
def refresh_pool(client: PoliteClient, store: Store, cfg: Config, today: str) -> list[dict]:
    old = {r["id"]: r for r in store.load_pool()}
    rows, page, total = [], 1, None
    while total is None or (page - 1) * bgg.POOL_PAGE_SIZE < total:
        items, total = bgg.fetch_pool_page(client, cfg.mechanic_id, page)
        if not items:
            break
        rows.extend(items)
        page += 1
    for r in rows:
        r["first_seen"] = old.get(r["id"], {}).get("first_seen") or today
    store.save_pool(rows)
    log(f"pool refreshed: {len(rows)} games ({len(rows) - len(old.keys() & {r['id'] for r in rows})} new)")
    return rows


def merge_details(games: dict[int, dict], things: list[dict], cfg: Config, today: str) -> None:
    for t in things:
        prev = games.get(t["id"], {})
        t["first_seen"] = prev.get("first_seen") or today
        t["details_at"] = today
        t["status"] = classify(t, cfg)
        games[t["id"]] = t


def run(cfg: Config, today: dt.date | None = None, client: PoliteClient | None = None) -> int:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    day = today.isoformat()
    store = Store(cfg.data_dir)
    client = client or PoliteClient(cfg)
    state, games = store.load_state(), store.load_games()
    hot: dict[int, int] = {}
    plays: dict[int, int] = {}
    exit_code = 0

    try:
        hot = bgg.fetch_hot(client)

        # 1. Mechanic listing: only re-read when stale.
        last = state.get("pool_refreshed")
        stale = not last or (today - dt.date.fromisoformat(last)).days >= cfg.pool_refresh_days
        pool = refresh_pool(client, store, cfg, day) if stale else store.load_pool()
        if stale:
            state["pool_refreshed"] = day
            store.save_state(state)
            for r in pool:  # free stat refresh for games outside the watchlist
                if r["id"] in games:
                    games[r["id"]].update({k: r[k] for k in STAT_KEYS})

        # 2. Details, once, for candidates we have never looked at.
        cand = [r["id"] for r in pool if is_candidate(r, cfg, today.year)]
        missing = [i for i in cand if i not in games]
        # ...plus a slow re-check of games whose weight was too thinly voted to judge.
        recheck_before = (today - dt.timedelta(days=30)).isoformat()
        recheck = [i for i, g in games.items() if g.get("status") == "unconfirmed"
                   and g.get("details_at", "") < recheck_before][: 5 * bgg.THING_BATCH]
        log(f"{len(cand)} candidates, {len(missing)} need details, {len(recheck)} due a re-check")
        for batch in chunks(missing + recheck, bgg.THING_BATCH):
            merge_details(games, bgg.fetch_things(client, batch), cfg, day)
            store.save_games(games)

        # 3. Fresh stats for the capped watchlist.
        watch = select_watchlist(games, hot, cfg, today.year)
        stale_ids = [i for i in watch if games[i].get("details_at") != day]
        for batch in chunks(stale_ids, bgg.THING_BATCH):
            merge_details(games, bgg.fetch_things(client, batch), cfg, day)
        store.save_games(games)
        watch = [i for i in watch if games[i]["status"] != "out"]

        # 4. Play counts for the window ending yesterday (complete days only).
        end = today - dt.timedelta(days=1)
        start = end - dt.timedelta(days=cfg.window_days - 1)
        for gid in watch:
            plays[gid] = bgg.fetch_plays_total(client, gid, start.isoformat(), end.isoformat())
    except BudgetExceeded as e:
        log(f"stopping early: {e}. Remaining work resumes next run.")
    except TooManyFailures as e:
        log(f"aborting: {e}")
        exit_code = 1

    store.save_games(games)
    if plays:
        rows = [{**{k: games[g].get(k) for k in ("id", "name", *STAT_KEYS)},
                 "plays": n, "hot_rank": hot.get(g)} for g, n in plays.items()]
        store.save_snapshot(day, rows)
    log(f"done: {client.requests_made} requests, {client.cache_hits} cache hits, "
        f"{client.failures} failures, {len(plays)} games snapshotted")
    return exit_code
