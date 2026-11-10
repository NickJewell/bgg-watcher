"""Turn the stored snapshots into WATCHLIST.md. Makes no network requests."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from pathlib import Path

from .config import Config
from .store import Store


def percentiles(values: dict[int, float]) -> dict[int, float]:
    """Rank-based 0..1 score per id (ties share the lower rank)."""
    if len(values) < 2:
        return {k: 0.0 for k in values}
    ordered = sorted(set(values.values()))
    pos = {v: i / (len(ordered) - 1) if len(ordered) > 1 else 0.0 for i, v in enumerate(ordered)}
    return {k: pos[v] for k, v in values.items()}


def heat_scores(snap: dict[int, dict]) -> dict[int, float]:
    """0..100 blend: are people playing it, playing it a lot relative to how
    many own it, wanting it, and looking at it right now."""
    plays = percentiles({i: r["plays"] or 0 for i, r in snap.items()})
    per_owner = percentiles({i: (r["plays"] or 0) / ((r["owned"] or 0) + 50) for i, r in snap.items()})
    want = percentiles({i: (r["wishing"] or 0) / ((r["owned"] or 0) + 50) for i, r in snap.items()})
    return {i: round(100 * (0.4 * plays[i] + 0.3 * per_owner[i] + 0.2 * want[i]
                            + (0.1 if snap[i]["hot_rank"] else 0)), 1) for i in snap}


def baseline_date(dates: list[str], latest: str, window_days: int) -> str | None:
    """Newest snapshot at least one full window older than the latest, so the
    two play windows do not overlap."""
    cutoff = (dt.date.fromisoformat(latest) - dt.timedelta(days=window_days)).isoformat()
    older = [d for d in dates if d <= cutoff]
    return older[-1] if older else None


def mechanic_trends(games: dict[int, dict], this_year: int, focus: str, min_count: int = 3):
    """Mechanics paired with the focus mechanic: share among recent in-scope
    releases vs older ones."""
    recent, older = Counter(), Counter()
    n_recent = n_older = 0
    for g in games.values():
        if g.get("status") == "out" or not g.get("year"):
            continue
        is_recent = g["year"] >= this_year - 2
        n_recent, n_older = n_recent + is_recent, n_older + (not is_recent)
        (recent if is_recent else older).update(m for m in g.get("mechanics", []) if m != focus)
    rows = []
    for mech, c in recent.items():
        if c < min_count or not n_recent or not n_older:
            continue
        share_r, share_o = c / n_recent, older[mech] / n_older
        rows.append((mech, c, share_r, share_o, share_r / share_o if share_o else float("inf")))
    # Rising mechanics only, most common first: raw lift alone favours rarities.
    rows = [r for r in rows if r[4] >= 1.5]
    rows.sort(key=lambda r: (-r[1], -r[4]))
    return rows, n_recent, n_older


def _table(header: list[str], rows: list[list]) -> list[str]:
    if not rows:
        return ["_Nothing yet._", ""]
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out + [""]


def build(cfg: Config, focus_mechanic: str = "Trick-taking") -> str:
    store = Store(cfg.data_dir)
    games, dates = store.load_games(), store.snapshot_dates()
    if not dates:
        return "# Trick-taking watchlist\n\nNo snapshots yet. Run `python -m bggwatch run` first.\n"
    latest = dates[-1]
    snap = store.load_snapshot(latest)
    year = dt.date.fromisoformat(latest).year
    base_day = baseline_date(dates, latest, cfg.window_days)
    base = store.load_snapshot(base_day) if base_day else {}
    heat = heat_scores(snap)

    def link(i):
        g = games.get(i, {})
        flag = " \\*" if g.get("status") == "unconfirmed" else ""
        return f"[{snap[i]['name']}](https://boardgamegeek.com/boardgame/{i}){flag}"

    def meta(i):
        g = games.get(i, {})
        return [g.get("year") or "?", f"{g.get('min_players')}–{g.get('max_players')}",
                f"{g.get('weight', 0):.2f}" if g.get("numweights") else "n/a"]

    def delta(i):
        if i not in base:
            return "–"
        return f"{(snap[i]['plays'] or 0) - (base[i]['plays'] or 0):+d}"

    def per100(i):
        return round(100 * (snap[i]["plays"] or 0) / max(snap[i]["owned"] or 0, 1), 1)

    L = [f"# Trick-taking watchlist — {latest}", "",
         f"Scope: trick-taking, playable at {cfg.min_players}–{cfg.max_players} players, "
         f"weight {cfg.min_weight}–{cfg.max_weight}. Plays = logged plays over the "
         f"{cfg.window_days} days before the snapshot. "
         + (f"Δ compares with {base_day}." if base_day else
            "Δ needs a snapshot at least one window older; it fills in as history accrues."),
         "", f"\\* weight unconfirmed (fewer than {cfg.min_weight_votes} votes).", ""]

    by_heat = sorted(snap, key=lambda i: -heat[i])
    L += ["## Top by heat", ""]
    L += _table(["Game", "Year", "Players", "Weight", "Plays", "Δ", "Plays/100 owners", "Wishlist", "Hot #", "Heat"],
                [[link(i), *meta(i), snap[i]["plays"], delta(i), per100(i), snap[i]["wishing"],
                  snap[i]["hot_rank"] or "", heat[i]] for i in by_heat[:25]])

    if base:
        movers = sorted((i for i in snap if i in base),
                        key=lambda i: -((snap[i]["plays"] or 0) - (base[i]["plays"] or 0)))
        L += ["## Biggest play gains", ""]
        L += _table(["Game", "Plays", "Δ plays", "Δ owned", "Δ wishlist"],
                    [[link(i), snap[i]["plays"], delta(i),
                      f"{(snap[i]['owned'] or 0) - (base[i]['owned'] or 0):+d}",
                      f"{(snap[i]['wishing'] or 0) - (base[i]['wishing'] or 0):+d}"] for i in movers[:15]])

    sleepers = sorted((i for i in snap if (snap[i]["owned"] or 0) < 2000 and (snap[i]["plays"] or 0) >= 5),
                      key=lambda i: -per100(i))
    L += ["## Sleepers", "", "Under 2,000 owners, played often by the people who have it.", ""]
    L += _table(["Game", "Year", "Players", "Weight", "Plays", "Owned", "Plays/100 owners"],
                [[link(i), *meta(i), snap[i]["plays"], snap[i]["owned"], per100(i)] for i in sleepers[:15]])

    wanted = sorted((i for i in snap if (games.get(i, {}).get("year") or 0) >= year - 1
                     and (snap[i]["wishing"] or 0) >= 20),
                    key=lambda i: -(snap[i]["wishing"] or 0) / ((snap[i]["owned"] or 0) + 50))
    L += ["## Anticipated", "", "Recent or upcoming releases with wishlists outrunning ownership.", ""]
    L += _table(["Game", "Year", "Players", "Weight", "Wishlist", "Owned"],
                [[link(i), *meta(i), snap[i]["wishing"], snap[i]["owned"]] for i in wanted[:15]])

    first_run = min((g.get("first_seen") or latest) for g in games.values()) if games else latest
    cutoff = (dt.date.fromisoformat(latest) - dt.timedelta(days=30)).isoformat()
    fresh = sorted((g for g in games.values() if g.get("status") != "out"
                    and (g.get("first_seen") or "") > max(first_run, cutoff)),
                   key=lambda g: g["first_seen"], reverse=True)
    L += ["## New to the pool (last 30 days)", ""]
    L += _table(["Game", "Year", "Players", "First seen"],
                [[f"[{g['name']}](https://boardgamegeek.com/boardgame/{g['id']})", g.get("year") or "?",
                  f"{g['min_players']}–{g['max_players']}", g["first_seen"]] for g in fresh[:20]])

    mech, n_recent, n_older = mechanic_trends(games, year, focus_mechanic)
    L += ["## Mechanics to watch", "",
          f"Mechanics paired with trick-taking among in-scope games: share of {year - 2}+ releases "
          f"({n_recent} games) against earlier ones ({n_older} games), for mechanics at least "
          "1.5× more common in recent releases.", ""]
    L += _table(["Mechanic", "Recent games", "Recent share", "Earlier share", "Lift"],
                [[m, c, f"{sr:.1%}", f"{so:.1%}", "new" if lift == float("inf") else f"{lift:.1f}×"]
                 for m, c, sr, so, lift in mech[:15]])

    designers = Counter(d for g in games.values() if g.get("status") != "out"
                        and (g.get("year") or 0) >= year - 3
                        for d in g.get("designers", []) if d != "(Uncredited)")
    L += ["## Active designers", "", f"In-scope releases since {year - 3}.", ""]
    L += _table(["Designer", "Games"], [[d, c] for d, c in designers.most_common(12) if c >= 2])

    scoped = Counter(g.get("status") for g in games.values())
    L += ["---", f"{scoped['confirmed']} confirmed and {scoped['unconfirmed']} weight-unconfirmed games in scope; "
          f"{len(snap)} tracked in this snapshot. Data from BoardGameGeek."]
    return "\n".join(L) + "\n"


def write(cfg: Config, path: str = "WATCHLIST.md") -> None:
    Path(path).write_text(build(cfg), encoding="utf-8")
