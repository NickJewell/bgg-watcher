# Weekly design analysis

Instructions for the scheduled Claude Code session that produces
`ANALYSIS_SNAPSHOT.md`. Work from the repository root on an up-to-date `main`.

The goal is a document a game designer can act on: what is driving the current
hot trick-taking games, a hundred new designs remixed from that, and designer
pitches for the ten most likely to reach a near-mainstream audience. The Python
steps below hold the constraints and do the arithmetic; the thinking is yours.
Each step prints what to do next, and exits non-zero with the reason if what
you wrote does not pass.

1. `python -m bggwatch analyse brief`, then read `.analysis/brief.md` and write
   `.analysis/generated.json`. Write all hundred variants yourself, properly:
   no script-generated or templated combinations, and spread them across the
   design space.
2. `python -m bggwatch analyse check`. If it rejects variants, fix or replace
   those and run it again until it passes.
3. Scoring must be independent of the writing. Start a fresh sub-agent, tell it
   only to read `.analysis/score-brief.md` and follow it, and let it write
   `.analysis/scores.json`. Tell it to judge each pitch on its merits, not to
   derive scores from a formula or keyword rules. Do not score the variants
   yourself and do not edit the scores afterwards.
4. `python -m bggwatch analyse rank`, then read `.analysis/pitch-brief.md` and
   write `.analysis/pitches.json`.
5. `python -m bggwatch analyse render`, then read `ANALYSIS_SNAPSHOT.md` through
   once to check it is complete and reads well.
6. Commit only `ANALYSIS_SNAPSHOT.md` with the message
   `Design analysis YYYY-MM-DD` (the snapshot date) and push to `main`. If the
   push is rejected because `main` moved, `git pull --rebase origin main` and
   push again.

If a step cannot be completed, leave the existing `ANALYSIS_SNAPSHOT.md` as it
is, push nothing, and report what went wrong.
