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
    if cfg.family_of(game) != cfg.families[0] and cfg.card_category not in (game.get("categories") or []):
        return "out"  # the other families are only wanted as card games
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
    """The primary family's quota first, then a smaller quota for each other family."""
    scoped = [g for g in games.values() if g.get("status") in ("confirmed", "unconfirmed")]
    scoped.sort(key=lambda g: (-priority(g, hot, this_year), g["id"]))
    out = []
    for n, family in enumerate(cfg.families):
        quota = cfg.family_watch_size if n else cfg.watchlist_size
        out += [g["id"] for g in scoped if cfg.family_of(g) == family][:quota]
    return out


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


STUB_KEYS = ("id", "name", "type", "year", "status", "first_seen", "details_at")


def merge_details(games: dict[int, dict], things: list[dict], cfg: Config, today: str,
                  mechanic_ids: dict[str, int] | None = None) -> None:
    for t in things:
        ids = t.pop("mechanic_ids", {})
        if mechanic_ids is not None:  # how the other families' BGG ids are learned
            mechanic_ids.update({k: v for k, v in ids.items() if k in cfg.families and v})
        prev = games.get(t["id"], {})
        t["first_seen"] = prev.get("first_seen") or today
        t["details_at"] = today
        t["status"] = classify(t, cfg)
        if t["status"] == "out" and cfg.family_of(t) != cfg.families[0]:
            t = {k: t.get(k) for k in STUB_KEYS}  # remember it was looked at, without storing it
        games[t["id"]] = t


def refresh_family_pool(client: PoliteClient, store: Store, cfg: Config, state: dict,
                        family: str, mechanic_id: int, today: str) -> bool:
    """Read another family's mechanic listing, at most pool_pages_per_run pages
    a run. Progress is kept in state and a .part.csv, so a long listing, or one
    cut short by the request budget, carries on the next day. True when complete."""
    st = state.setdefault("pools", {}).setdefault(family, {})
    page = st.get("next_page", 1)
    rows = store.load_pool(family, partial=True) if page > 1 else []
    total, done = st.get("size"), False
    try:
        for _ in range(cfg.pool_pages_per_run):
            items, total = bgg.fetch_pool_page(client, mechanic_id, page)
            rows.extend(items)
            page += 1
            if not items or (page - 1) * bgg.POOL_PAGE_SIZE >= total:
                done = True
                break
    finally:
        st["size"] = total
        if done:
            old = {r["id"]: r for r in store.load_pool(family)}
            for r in rows:
                r["first_seen"] = old.get(r["id"], {}).get("first_seen") or today
            store.save_pool(rows, family)
            st.pop("next_page", None)
            st["refreshed"] = today
            log(f"{family}: pool refreshed, {len(rows)} games")
        else:
            store.save_pool(rows, family, partial=True)
            st["next_page"] = page
            log(f"{family}: pool listing at page {page - 1} of {math.ceil((total or 0) / bgg.POOL_PAGE_SIZE) or '?'}")
        store.save_state(state)
    return done


def crawl_family(client: PoliteClient, store: Store, cfg: Config, state: dict, games: dict[int, dict],
                 family: str, mech_ids: dict[str, int], today: dt.date) -> None:
    """One of the other families: re-read its listing if due, then fetch
    details for candidates not looked at before."""
    day = today.isoformat()
    if family not in mech_ids:
        log(f"{family}: BGG id not seen yet (it is learned from a watched game that has the mechanic)")
        return
    st = state.get("pools", {}).get(family, {})
    last = st.get("refreshed")
    if "next_page" in st or not last or (today - dt.date.fromisoformat(last)).days >= cfg.pool_refresh_days:
        if not refresh_family_pool(client, store, cfg, state, family, mech_ids[family], day):
            return
    new = [r["id"] for r in store.load_pool(family)
           if r["id"] not in games and is_candidate(r, cfg, today.year)]
    if new:
        log(f"{family}: {len(new)} candidates need details")
    for batch in chunks(new, bgg.THING_BATCH):
        merge_details(games, bgg.fetch_things(client, batch), cfg, day, mech_ids)
        store.save_games(games)


def run(cfg: Config, today: dt.date | None = None, client: PoliteClient | None = None) -> int:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    day = today.isoformat()
    store = Store(cfg.data_dir)
    client = client or PoliteClient(cfg)
    state, games = store.load_state(), store.load_games()
    mech_ids = state.setdefault("mechanic_ids", {})
    mech_ids[cfg.families[0]] = cfg.mechanic_id
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
            merge_details(games, bgg.fetch_things(client, batch), cfg, day, mech_ids)
            store.save_games(games)

        # 3. Fresh stats for the capped watchlist.
        watch = select_watchlist(games, hot, cfg, today.year)
        stale_ids = [i for i in watch if games[i].get("details_at") != day]
        for batch in chunks(stale_ids, bgg.THING_BATCH):
            merge_details(games, bgg.fetch_things(client, batch), cfg, day, mech_ids)
        store.save_games(games)
        watch = [i for i in watch if games[i]["status"] != "out"]

        # 4. Play counts for the window ending yesterday (complete days only).
        end = today - dt.timedelta(days=1)
        start = end - dt.timedelta(days=cfg.window_days - 1)
        for gid in watch:
            plays[gid] = bgg.fetch_plays_total(client, gid, start.isoformat(), end.isoformat())

        # 5. The other families, last, so they only ever spend what the daily
        #    snapshot left over. Their games join the watchlist on the next run.
        for family in cfg.families[1:]:
            try:
                crawl_family(client, store, cfg, state, games, family, mech_ids, today)
            except (BudgetExceeded, TooManyFailures):
                raise
            except Exception as e:  # never let an extra family cost the day its snapshot
                log(f"{family}: skipped today ({e})")
    except BudgetExceeded as e:
        log(f"stopping early: {e}. Remaining work resumes next run.")
    except TooManyFailures as e:
        log(f"aborting: {e}")
        exit_code = 1

    store.save_games(games)
    store.save_state({**store.load_state(), "mechanic_ids": mech_ids})
    if plays:
        rows = [{**{k: games[g].get(k) for k in ("id", "name", *STAT_KEYS)},
                 "plays": n, "hot_rank": hot.get(g)} for g, n in plays.items()]
        store.save_snapshot(day, rows)
    log(f"done: {client.requests_made} requests, {client.cache_hits} cache hits, "
        f"{client.failures} failures, {len(plays)} games snapshotted")
    return exit_code


def estimate_cost(cfg: Config, store: Store) -> str:
    """Requests per run, from the configuration and the listing sizes seen so far."""
    state = store.load_state()
    watch = cfg.watchlist_size + cfg.family_watch_size * (len(cfg.families) - 1)
    daily = 1 + math.ceil(watch / bgg.THING_BATCH) + watch
    L = [f"Daily: 1 hot list + {math.ceil(watch / bgg.THING_BATCH)} stat requests + {watch} play counts "
         f"= {daily} requests ({cfg.watchlist_size} {cfg.families[0]} games"
         + "".join(f", {cfg.family_watch_size} {f}" for f in cfg.families[1:]) + ").",
         "Mechanic listings, re-read every " f"{cfg.pool_refresh_days} days:"]
    weekly, unknown = 0, []
    for n, family in enumerate(cfg.families):
        size = len(store.load_pool()) if n == 0 else (state.get("pools", {}).get(family, {}).get("size"))
        if not size:
            unknown.append(family)
            L.append(f"  {family}: size not known yet (BGG id {state.get('mechanic_ids', {}).get(family, 'not learned yet')})")
            continue
        pages = math.ceil(size / bgg.POOL_PAGE_SIZE)
        weekly += pages
        days = 1 if n == 0 else math.ceil(pages / cfg.pool_pages_per_run)
        L.append(f"  {family}: {size} games, {pages} requests" + (f", spread over {days} days" if days > 1 else ""))
    seconds = cfg.min_delay + cfg.jitter / 2
    L += [f"Listings total {weekly} requests a week" + (f", not counting {', '.join(unknown)}" if unknown else "")
          + f"; averaged over the week that is {daily + math.ceil(weekly / cfg.pool_refresh_days)} requests a day.",
          f"Budget: {cfg.request_budget} requests a run, which is about "
          f"{round(cfg.request_budget * seconds / 60)} minutes at one request every {seconds:.0f} seconds. "
          "Details for newly listed candidates are one-off, 20 games a request, and wait for spare budget."]
    return "\n".join(L)
