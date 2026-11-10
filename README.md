# bgg-trick-watch

A small, polite BoardGameGeek watcher for one niche: **trick-taking card games,
playable at 2–4 players, weight 1.5–2.0**. Once a day it takes a snapshot of a
capped watchlist and regenerates [`WATCHLIST.md`](WATCHLIST.md): what is being
played, what is gaining, what is anticipated, and which mechanics are showing up
alongside trick-taking in new releases.

No dependencies beyond Python 3.10+.

```sh
export BGG_API_TOKEN=...        # see "The API token" below
python -m bggwatch run          # fetch today's snapshot, rewrite WATCHLIST.md
python -m bggwatch report       # rebuild WATCHLIST.md from stored data, no network
python -m unittest discover -s tests
```

## What it fetches

| Step | Endpoint | Requests | How often |
|---|---|---|---|
| Hot list | `xmlapi2/hot` | 1 | daily |
| Every game tagged Trick-taking, with headline stats | `api/geekitem/linkeditems` | ~67 (50 games per page) | weekly |
| Player counts, weight votes, mechanics, designers | `xmlapi2/thing`, 20 ids per request | one-off per new candidate | as games appear |
| Fresh stats for the watchlist | `xmlapi2/thing` | ~8 | daily |
| Logged plays in the last 7 days | `xmlapi2/plays`, page 1 only | 1 per watched game (150) | daily |

A normal day is about 160 requests; the weekly pool refresh adds about 67.

The play count comes from the `total` attribute on the first page of a
date-windowed query, so individual play records are never paged through. BGG
exposes no history, so trends come from comparing this repo's own snapshots.

`api/geekitem/linkeditems` is the JSON endpoint behind BGG's own mechanic pages.
It is undocumented and may change; the XML API has no way to list games by
mechanic. Everything else uses the documented XML API2.

## Being a good citizen

All of this lives in `bggwatch/client.py` and is covered by tests.

- **Hard request budget** per run (300). When it is spent the run saves what it
  has and stops; the remainder is picked up next time.
- **One request at a time**, 2.5–3.5 seconds apart.
- **Backoff** on 202, 429 and 5xx (5s, 10s, 20s, 40s, or `Retry-After` if
  longer). 4xx responses are not retried.
- **Abort** after 5 failed requests in a run.
- **20-hour response cache**, so re-runs and development cost nothing.
- **Capped watchlist**: only 150 games get per-game requests, chosen by
  wishlist and ownership, weighted towards recent releases, with anything on the
  hot list included first.

Tunables are in `bggwatch/config.py`; each can be overridden with a
`BGGWATCH_<NAME>` environment variable (for example `BGGWATCH_WATCHLIST_SIZE=80`).

## Scope rules

- **Players**: in scope if the published player range overlaps 2–4.
- **Weight**: 1.5–2.0 with at least 5 weight votes is *confirmed*. Fewer votes
  is *weight unconfirmed* (marked `*` in the report) and re-checked monthly,
  because new releases rarely have enough votes to judge.
- Expansions are excluded.

## The API token

BGG requires a registered application token for its XML API. The code reads it
from the `BGG_API_TOKEN` environment variable and sends it as a bearer header.
It is never written to disk, logged, or cached.

- Locally: `export BGG_API_TOKEN=...`, or keep it in `.env` (gitignored) and
  source it.
- GitHub Actions: add it under *Settings → Secrets and variables → Actions* as
  `BGG_API_TOKEN`. The included workflow runs daily and commits `data/` and
  `WATCHLIST.md`.

## Data

```
data/pool.csv              every trick-taking game on BGG, refreshed weekly
data/games.json            details and scope status for candidates
data/snapshots/DATE.csv    one row per watched game per day
data/state.json            when the pool was last refreshed
```

## Heat score

A 0–100 blend of percentile ranks within the snapshot: plays in the window
(40%), plays per owner (30%), wishlist per owner (20%), and a flat 10% for being
on BGG's hot list. It is a sorting aid, not a measurement.
