# bgg-watcher

A small, polite BoardGameGeek watcher for one niche: **trick-taking card games,
playable at 2–4 players, weight 1.5–2.0**. Once a day it takes a snapshot of a
capped watchlist and regenerates [`WATCHLIST.md`](WATCHLIST.md): what is being
played, what is gaining, what is anticipated, and which mechanics are showing up
alongside trick-taking in new releases.

Trick-taking is the primary family. A smaller watch is kept on card games from
three neighbouring families (ladder climbing, push your luck, set collection),
same player count and weight, so the weekly design analysis has more than one
kind of game to draw on. The list is `families` in `bggwatch/config.py`.

No dependencies beyond Python 3.10+.

```sh
export BGG_API_TOKEN=...        # see "The API token" below
python -m bggwatch run          # fetch today's snapshot, rewrite WATCHLIST.md
python -m bggwatch report       # rebuild WATCHLIST.md from stored data, no network
python -m bggwatch cost         # estimate requests per run from stored data, no network
python -m unittest discover -s tests
```

## What it fetches

| Step | Endpoint | Requests | How often |
|---|---|---|---|
| Hot list | `xmlapi2/hot` | 1 | daily |
| Every game tagged Trick-taking, with headline stats | `api/geekitem/linkeditems` | ~67 (50 games per page) | weekly |
| Player counts, weight votes, mechanics, designers | `xmlapi2/thing`, 20 ids per request | one-off per new candidate | as games appear |
| Fresh stats for the watchlist | `xmlapi2/thing` | ~14 | daily |
| Logged plays in the last 7 days | `xmlapi2/plays`, page 1 only | 1 per watched game (150 trick-taking, 40 for each other family) | daily |
| Every game tagged with each other family's mechanic | `api/geekitem/linkeditems` | 50 games per page, at most 40 pages per family per day | weekly, resumed over several days if long |

A normal day is about 285 requests once all four families are being watched;
the weekly trick-taking listing adds about 67. The other families' listings
and the one-off details for their candidates are fetched last, with whatever
is left of the day's budget, so they can never cost the daily snapshot its
play counts. `python -m bggwatch cost` prints the current estimate, including
each listing's size once it has been read.

The other families are found by name: their BGG mechanic ids are read from the
details of watched games that carry them, never hard-coded. Their games must
also be in BGG's Card Game category, since those mechanics cover far more than
card games. Games that fail that test are remembered as a one-line stub.

The play count comes from the `total` attribute on the first page of a
date-windowed query, so individual play records are never paged through. BGG
exposes no history, so trends come from comparing this repo's own snapshots.

`api/geekitem/linkeditems` is the JSON endpoint behind BGG's own mechanic pages.
It is undocumented and may change; the XML API has no way to list games by
mechanic. Everything else uses the documented XML API2.

## Design analysis

Once a week a scheduled Claude Code session reads the latest snapshot and
overwrites [`ANALYSIS_SNAPSHOT.md`](ANALYSIS_SNAPSHOT.md) and keeps a dated
copy of each week in [`analyses/`](analyses), following
[`ANALYSIS_TASK.md`](ANALYSIS_TASK.md). It runs on a Claude subscription, makes
no BGG requests and calls no paid API.

1. **Extract and remix.** The 24 hottest games, an equal share from each
   family that has games in the snapshot and ranked by heat within the family,
   plus anything on BGG's hot list, go in with their stats, tags and
   descriptions. A quarter of each share is held for games that were not
   sources the week before. Claude pulls out the top mechanics, themes and
   differentiators, then remixes them into 100 new variants, each labelled
   with a family or as a hybrid of two.
2. **Score.** A fresh sub-agent that has not seen how the variants were written
   scores each one 1–10 on seven dimensions: three from Koster's Theory of Fun
   (pattern novelty, mastery depth, learning feedback), three from MDA
   (mechanics, dynamics, aesthetics), and mainstream reach. Weights are 35%
   Koster, 35% MDA, 30% market.
3. **Pitch.** The top ten get a two-to-three paragraph designer pitch. With
   more than one family in play, no family (and no group of hybrids) takes
   more than three of the ten places.

`python -m bggwatch analyse brief|check|rank|render` is the scaffolding between
those steps. The code, not the model, enforces the constraints (player range
inside 2–4, weight 1.5–2.0, no duplicate titles, a minimum and maximum number
of variants per family), requires a score for every variant, computes the
weighted totals and picks the top ten.

`analyses/index.json` records what each week used and produced. The brief
lists the designs from the last six weeks so they are not written again, and
the check rejects any title used before and any core twist that shares most of
its wording with one from those weeks. That catches a reworded copy; it cannot
tell that two differently worded rules are the same idea. The full ranked
list of 100 is kept in a collapsed section at the bottom of the file.

The scores are one model's judgement against a rubric. They are good for
sorting a hundred ideas; they are not a sales forecast.

## Being a good citizen

All of this lives in `bggwatch/client.py` and is covered by tests.

- **Hard request budget** per run (450). When it is spent the run saves what it
  has and stops; the remainder is picked up next time.
- **One request at a time**, 2.5–3.5 seconds apart.
- **Backoff** on 202, 429 and 5xx (5s, 10s, 20s, 40s, or `Retry-After` if
  longer). 4xx responses are not retried.
- **Abort** after 5 failed requests in a run.
- **20-hour response cache**, so re-runs and development cost nothing.
- **Capped watchlist**: only 150 trick-taking games, and 40 from each other
  family, get per-game requests, chosen by wishlist and ownership, weighted
  towards recent releases, with anything on the hot list included first.

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
data/pools/FAMILY.csv      the same listing for each other family
data/games.json            details and scope status for candidates
data/snapshots/DATE.csv    one row per watched game per day
data/state.json            when each listing was last read, its size, and the families' BGG ids
analyses/DATE.md           the design analysis for that snapshot, one per week
analyses/index.json        each week's sources and variants, used to avoid repeats
```

## Heat score

A 0–100 blend of percentile ranks within the snapshot: plays in the window
(40%), plays per owner (30%), wishlist per owner (20%), and a flat 10% for being
on BGG's hot list. It is a sorting aid, not a measurement.
