# Weekly design analysis

Instructions for the scheduled Claude Code session that produces
`ANALYSIS_SNAPSHOT.md` (always the latest), a dated copy of it in
`analyses/YYYY-MM-DD.md`, so earlier weeks stay readable, and
`analyses/index.json`, the record of what every week used and produced. Work from the
repository root on an up-to-date `main`.

The goal is a document a game designer can act on: what is driving the current
hot card games in the families being watched (trick-taking first, and whichever
of the others have games in the snapshot), a hundred new designs remixed from
that, and designer
pitches for the ten most likely to reach a near-mainstream audience. The Python
steps below hold the constraints and do the arithmetic; the thinking is yours.
Each step prints what to do next, and exits non-zero with the reason if what
you wrote does not pass.

1. `python -m bggwatch analyse brief`, then read `.analysis/brief.md` and write
   `.analysis/generated.json`. Write all hundred variants yourself, properly:
   no script-generated or templated combinations, and spread them across the
   design space. The brief says which families are in play, how many variants
   each needs, and lists the designs from earlier weeks, which must not come
   back under any title.
2. `python -m bggwatch analyse check`. If it rejects variants (including for
   repeating an earlier week, or for an uneven split between families), fix or
   replace those and run it again until it passes.
3. Scoring must be independent of the writing. Start a fresh sub-agent, tell it
   only to read `.analysis/score-brief.md` and follow it, and let it write
   `.analysis/scores.json`. Tell it to judge each pitch on its merits, not to
   derive scores from a formula or keyword rules. Do not score the variants
   yourself and do not edit the scores afterwards.
4. `python -m bggwatch analyse rank`, then read `.analysis/pitch-brief.md` and
   write `.analysis/pitches.json`. The ten to pitch are chosen by the code: the
   highest totals, with a limit on how many one family may take. Pitch exactly
   those.
5. `python -m bggwatch analyse render`, then read `ANALYSIS_SNAPSHOT.md` through
   once to check it is complete and reads well. Render also writes the same
   text to `analyses/YYYY-MM-DD.md`, named for the snapshot date, and adds the
   week to `analyses/index.json`; if you change a pitch and render again, all
   three are rewritten.
6. Commit `ANALYSIS_SNAPSHOT.md`, the new `analyses/YYYY-MM-DD.md` and
   `analyses/index.json`, those three files only, with the message
   `Design analysis YYYY-MM-DD` (the snapshot date) and push to `main`. If the push is rejected because `main` moved,
   `git pull --rebase origin main` and push again.

If a step cannot be completed, leave the existing `ANALYSIS_SNAPSHOT.md` and
`analyses/` as they are, push nothing, and report what went wrong.
