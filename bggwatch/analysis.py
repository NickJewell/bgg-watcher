"""Ludological analysis of the latest snapshot, written to ANALYSIS_SNAPSHOT.md.

Three separate Claude calls, with the ranking done here in code:

  1. read the hot/active games, extract signals, remix them into N variants
  2. score every variant against a fixed rubric (a fresh call, so the scorer
     is not grading its own freshly written prose in the same breath)
  3. write a designer pitch for the top K by weighted score

Needs the `anthropic` package and an ANTHROPIC_API_KEY in the environment.
Makes no BoardGameGeek requests.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from .config import Config
from .report import heat_scores
from .store import Store

OUTPUT = "ANALYSIS_SNAPSHOT.md"

# dimension -> (framework, weight, what a high score means)
RUBRIC = {
    "pattern_novelty": ("Koster", 0.12, "offers a genuinely new pattern to learn, not a reskin of a known one"),
    "mastery_depth": ("Koster", 0.13, "the pattern keeps teaching: skill grows over many plays before it is solved"),
    "learning_feedback": ("Koster", 0.10, "players can see why they won or lost a trick and improve; neither noise nor a solved puzzle"),
    "mechanics": ("MDA", 0.10, "few, clean rules; teachable in under five minutes; no fiddly exceptions"),
    "dynamics": ("MDA", 0.13, "the rules produce interesting table behaviour: reads, bluffs, tempo, shifting alliances"),
    "aesthetics": ("MDA", 0.12, "delivers a clear, intended feeling (challenge, fellowship, discovery, expression...) reliably"),
    "mainstream_reach": ("Market", 0.30, "one-sentence hook, broadly appealing theme, cheap to produce, works across 2-4 players"),
}
assert abs(sum(w for _, w, _ in RUBRIC.values()) - 1) < 1e-9


class AnalysisError(RuntimeError):
    pass


# -- inputs ---------------------------------------------------------------------
def select_sources(cfg: Config, store: Store) -> tuple[str, list[dict]]:
    """The hot/active games: top by heat in the latest snapshot, plus anything
    on BGG's hot list."""
    dates = store.snapshot_dates()
    if not dates:
        raise AnalysisError("no snapshots yet; run `python -m bggwatch run` first")
    snap, games = store.load_snapshot(dates[-1]), store.load_games()
    heat = heat_scores(snap)
    ranked = sorted(snap, key=lambda i: -heat[i])
    chosen = ranked[: cfg.analysis_sources] + [i for i in ranked[cfg.analysis_sources:] if snap[i]["hot_rank"]]
    out = []
    for i in chosen:
        g = games.get(i, {})
        out.append({
            "name": snap[i]["name"], "year": g.get("year"),
            "players": f"{g.get('min_players')}-{g.get('max_players')}",
            "weight": g.get("weight") if g.get("numweights") else None,
            "mechanics": g.get("mechanics", []), "categories": g.get("categories", []),
            "designers": g.get("designers", []),
            "plays_7d": snap[i]["plays"], "owned": snap[i]["owned"], "wishlist": snap[i]["wishing"],
            "bgg_hot_rank": snap[i]["hot_rank"], "heat": heat[i],
            "description": g.get("description", ""),
        })
    return dates[-1], out


# -- prompts and schemas --------------------------------------------------------
def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_STR, _INT, _NUM = {"type": "string"}, {"type": "integer"}, {"type": "number"}
_STRS = {"type": "array", "items": _STR}
_SIGNAL = {"type": "array", "items": _obj({"name": _STR, "seen_in": _STRS, "why_it_matters": _STR})}
VARIANT = _obj({"title": _STR, "hook": _STR, "core_twist": _STR, "theme": _STR, "remixes": _STRS,
                "mechanics": _STRS, "players_min": _INT, "players_max": _INT, "est_weight": _NUM})
GENERATE_SCHEMA = _obj({"mechanics": _SIGNAL, "themes": _SIGNAL, "differentiators": _SIGNAL,
                        "variants": {"type": "array", "items": VARIANT}})
TOPUP_SCHEMA = _obj({"variants": {"type": "array", "items": VARIANT}})
SCORE_SCHEMA = _obj({"scores": {"type": "array", "items": _obj(
    {"id": _INT, **{k: _INT for k in RUBRIC}, "rationale": _STR})}})
PITCH_SCHEMA = _obj({"pitches": {"type": "array", "items": _obj({"id": _INT, "pitch": _STR})}})

SYSTEM = (
    "You are a tabletop game designer and ludologist who specialises in trick-taking card games. "
    "You know the modern trick-taking scene well, from traditional games to the current wave of "
    "small-box Japanese and Western designs, and you reason about games through Raph Koster's "
    "Theory of Fun (fun is the pleasure of learning and mastering patterns; a game dies when its "
    "pattern is either solved or illegible) and the MDA framework (designers set Mechanics, which "
    "give rise to Dynamics at the table, which players experience as Aesthetics). "
    "Your reader is a designer deciding what to prototype next, so be concrete and specific: name "
    "the actual rule, not the genre."
)


def constraints(cfg: Config) -> str:
    return (f"Every design must be a trick-taking card game at its core, playable by "
            f"{cfg.min_players} to {cfg.max_players} players (its whole supported range must sit inside "
            f"that), with a BoardGameGeek-style complexity weight between {cfg.min_weight} and "
            f"{cfg.max_weight} (light: a short rules teach, but real decisions).")


def generate_prompt(cfg: Config, sources: list[dict], n: int) -> str:
    return (
        "Below are the trick-taking games currently drawing the most activity on BoardGameGeek, with "
        "play counts for the last week, ownership, wishlists, tags and a short description. The tags "
        "are coarse, so read the descriptions for what actually sets each game apart.\n\n"
        f"<games>\n{json.dumps(sources, ensure_ascii=False, indent=1)}\n</games>\n\n"
        "First, extract what is driving this set: the top mechanics, the top themes, and the top "
        "differentiators (the specific rule, component or framing that makes a game stand out from "
        "ordinary trick-takers). For each, list the games it appears in and say why it matters.\n\n"
        f"Then remix those signals into {n} novel game variants. A variant combines two or more of the "
        "signals you found, or pushes one of them somewhere none of these games took it. It must be a "
        "new design, not a reskin or a house rule of one existing game, and the variants should be "
        "spread across the design space rather than clustered around one idea: vary the core twist, "
        "the theme, the player interaction and the scoring. " + constraints(cfg) + "\n\n"
        "For each variant give a title, a one-sentence hook a publisher could put on the box, the "
        "core twist as an actual rule, the theme, which source games or signals it remixes, its "
        "mechanics, its player range and an honest estimated weight."
    )


def topup_prompt(cfg: Config, existing: list[dict], n: int) -> str:
    titles = "\n".join(f"- {v['title']}: {v['core_twist']}" for v in existing)
    return (f"These variants already exist:\n{titles}\n\nWrite {n} more that are clearly different from "
            "all of them, in the same format. " + constraints(cfg))


def score_prompt(variants: list[dict]) -> str:
    rubric = "\n".join(f"- {k} ({fw}): 10 means it {desc}." for k, (fw, _, desc) in RUBRIC.items())
    slim = [{k: v[k] for k in ("id", "title", "hook", "core_twist", "theme", "mechanics",
                               "players_min", "players_max", "est_weight")} for v in variants]
    return (
        "Score each of these trick-taking game pitches for its chance of near-mainstream success: the "
        "kind of game that crosses from hobbyist circles into wide retail and gets played by people "
        "who do not think of themselves as gamers.\n\n"
        f"Give an integer from 1 to 10 on each dimension:\n{rubric}\n\n"
        "These scores are used to cut the list down to the best tenth, so they only help if they "
        "discriminate. Use the whole scale, judge each pitch against the others, and expect most to "
        "land in the middle. A clever idea that would confuse a family at the table should score low "
        "on mechanics and mainstream_reach however novel it is. Add a one-sentence rationale naming "
        "the pitch's biggest strength and biggest risk. Return a score for every id.\n\n"
        f"<pitches>\n{json.dumps(slim, ensure_ascii=False, indent=1)}\n</pitches>"
    )


def pitch_prompt(cfg: Config, top: list[dict]) -> str:
    return (
        "These are the ten strongest of a hundred candidate designs, with the scores and rationale "
        "they were given. Write a game designer's pitch for each, two to three paragraphs of plain "
        "prose separated by blank lines. Cover how a round actually plays and where the central "
        "decision sits, what players learn over repeated plays and why that stays interesting "
        "(Koster), how the rules turn into table dynamics and what feeling that produces (MDA), and "
        "why it could reach a wide audience, including the biggest design risk and how you would "
        "playtest for it. " + constraints(cfg) + " Stay consistent with each design's core twist; "
        "you may sharpen details.\n\n"
        f"<designs>\n{json.dumps(top, ensure_ascii=False, indent=1)}\n</designs>"
    )


# -- Claude ---------------------------------------------------------------------
def claude_call(cfg: Config):
    """Returns call(prompt, schema) -> parsed JSON, backed by the Claude API."""
    import anthropic  # imported here so the crawler itself stays dependency-free

    client = anthropic.Anthropic()  # credentials come from the environment

    def call(prompt: str, schema: dict) -> dict:
        try:
            with client.beta.messages.stream(
                model=cfg.analysis_model,
                max_tokens=64000,
                system=SYSTEM,
                thinking={"type": "adaptive"},
                output_config={"effort": cfg.analysis_effort,
                               "format": {"type": "json_schema", "schema": schema}},
                # If a safety classifier declines, let the API retry on its default fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                message = stream.get_final_message()
        except anthropic.AuthenticationError as e:
            raise AnalysisError("the Claude API rejected ANTHROPIC_API_KEY") from e
        except anthropic.RateLimitError as e:
            raise AnalysisError("Claude API rate limit reached; try again later") from e
        except anthropic.APIStatusError as e:
            raise AnalysisError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise AnalysisError("could not reach the Claude API") from e
        if message.stop_reason != "end_turn":
            raise AnalysisError(f"Claude stopped early ({message.stop_reason})")
        return json.loads(next(b.text for b in message.content if b.type == "text"))

    return call


# -- pure steps (unit tested) ---------------------------------------------------
def valid_variant(v: dict, cfg: Config) -> bool:
    return (cfg.min_players <= v["players_min"] <= v["players_max"] <= cfg.max_players
            and cfg.min_weight <= v["est_weight"] <= cfg.max_weight
            and bool(v["title"].strip()) and bool(v["core_twist"].strip()))


def add_variants(pool: list[dict], new: list[dict], cfg: Config, limit: int) -> None:
    """Append valid, non-duplicate variants up to `limit`, assigning ids."""
    seen = {v["title"].strip().lower() for v in pool}
    for v in new:
        key = v["title"].strip().lower()
        if len(pool) < limit and key not in seen and valid_variant(v, cfg):
            seen.add(key)
            pool.append({**v, "id": len(pool) + 1})


def total_score(s: dict) -> float:
    """Weighted 0-100 score; each dimension is clamped to 1-10."""
    return round(10 * sum(w * min(10, max(1, s[k])) for k, (_, w, _) in RUBRIC.items()), 1)


def framework_scores(s: dict) -> dict[str, float]:
    out = {}
    for fw in ("Koster", "MDA", "Market"):
        dims = [(w, s[k]) for k, (f, w, _) in RUBRIC.items() if f == fw]
        out[fw] = round(sum(w * min(10, max(1, v)) for w, v in dims) / sum(w for w, _ in dims), 1)
    return out


def rank(variants: list[dict], scores: list[dict]) -> list[dict]:
    by_id = {s["id"]: s for s in scores}
    scored = [{**v, "scores": by_id[v["id"]], "total": total_score(by_id[v["id"]])}
              for v in variants if v["id"] in by_id]
    return sorted(scored, key=lambda v: (-v["total"], v["id"]))


# -- orchestration ---------------------------------------------------------------
def analyse(cfg: Config, call) -> dict:
    store = Store(cfg.data_dir)
    date, sources = select_sources(cfg, store)
    n = cfg.analysis_variants

    first = call(generate_prompt(cfg, sources, n), GENERATE_SCHEMA)
    variants: list[dict] = []
    add_variants(variants, first["variants"], cfg, n)
    for _ in range(2):  # top up if some were invalid, duplicated or simply missing
        if len(variants) >= n:
            break
        more = call(topup_prompt(cfg, variants, n - len(variants)), TOPUP_SCHEMA)
        add_variants(variants, more["variants"], cfg, n)
    if len(variants) < cfg.analysis_keep:
        raise AnalysisError(f"only {len(variants)} valid variants were generated")

    ranked = rank(variants, call(score_prompt(variants), SCORE_SCHEMA)["scores"])
    if len(ranked) < cfg.analysis_keep:
        raise AnalysisError(f"only {len(ranked)} variants were scored")
    top = ranked[: cfg.analysis_keep]
    brief = [{k: v[k] for k in ("id", "title", "hook", "core_twist", "theme", "remixes", "mechanics",
                                "players_min", "players_max", "est_weight", "total")}
             | {"rationale": v["scores"]["rationale"]} for v in top]
    pitches = {p["id"]: p["pitch"] for p in call(pitch_prompt(cfg, brief), PITCH_SCHEMA)["pitches"]}
    missing = [v["title"] for v in top if not pitches.get(v["id"], "").strip()]
    if missing:
        raise AnalysisError(f"no pitch returned for: {', '.join(missing)}")
    for v in top:
        v["pitch"] = pitches[v["id"]].strip()
    return {"date": date, "sources": sources, "signals": {k: first[k] for k in ("mechanics", "themes", "differentiators")},
            "ranked": ranked, "top": top}


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render(result: dict, cfg: Config) -> str:
    top, ranked = result["top"], result["ranked"]
    L = [f"# Trick-taking design analysis — {result['date']}", "",
         f"Generated by {cfg.analysis_model} from the {len(result['sources'])} most active games in the "
         f"{result['date']} snapshot. {len(ranked)} variants were generated and scored; the top "
         f"{len(top)} are pitched below. Scores are the model's judgement against a fixed rubric, "
         "useful for sorting ideas, not a forecast.", "",
         "**Source games:** " + ", ".join(s["name"] for s in result["sources"]), ""]

    for key, title in (("mechanics", "Top mechanics"), ("themes", "Top themes"),
                       ("differentiators", "Top differentiators")):
        L += [f"## {title}", ""]
        L += [f"- **{_cell(s['name'])}** ({', '.join(s['seen_in'])}): {s['why_it_matters']}"
              for s in result["signals"][key]] + [""]

    L += [f"## Top {len(top)} variants", "",
          "| # | Variant | Players | Weight | Koster | MDA | Market | Total |", "|---|---|---|---|---|---|---|---|"]
    for n, v in enumerate(top, 1):
        f = framework_scores(v["scores"])
        L.append(f"| {n} | {_cell(v['title'])} | {v['players_min']}–{v['players_max']} | {v['est_weight']:.1f} "
                 f"| {f['Koster']} | {f['MDA']} | {f['Market']} | **{v['total']}** |")
    L.append("")
    for n, v in enumerate(top, 1):
        s = v["scores"]
        L += [f"### {n}. {v['title']} — {v['total']}/100", "", f"*{v['hook']}*", "",
              f"- **Core twist:** {v['core_twist']}",
              f"- **Theme:** {v['theme']}",
              f"- **Remixes:** {', '.join(v['remixes'])}",
              f"- **Players / weight:** {v['players_min']}–{v['players_max']}, {v['est_weight']:.1f}",
              "- **Scores:** " + ", ".join(f"{k.replace('_', ' ')} {s[k]}" for k in RUBRIC),
              f"- **Scorer's note:** {s['rationale']}", "", v["pitch"], ""]

    L += ["## Scoring rubric", "", "Each dimension is scored 1–10; the total is the weighted sum scaled to 100.", "",
          "| Dimension | Framework | Weight | A high score means it… |", "|---|---|---|---|"]
    L += [f"| {k.replace('_', ' ')} | {fw} | {w:.0%} | {desc} |" for k, (fw, w, desc) in RUBRIC.items()]
    L += ["", f"## All {len(ranked)} variants", "", "<details><summary>Full ranked list</summary>", "",
          "| Rank | Variant | Hook | Core twist | Total |", "|---|---|---|---|---|"]
    L += [f"| {n} | {_cell(v['title'])} | {_cell(v['hook'])} | {_cell(v['core_twist'])} | {v['total']} |"
          for n, v in enumerate(ranked, 1)]
    L += ["", "</details>", ""]
    return "\n".join(L)


def run(cfg: Config, call=None, path: str = OUTPUT) -> int:
    if call is None:
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            print("[bggwatch] analysis skipped: ANTHROPIC_API_KEY is not set", flush=True)
            return 0
        call = claude_call(cfg)
    try:
        result = analyse(cfg, call)
    except AnalysisError as e:
        print(f"[bggwatch] analysis failed: {e}", flush=True)
        return 1
    Path(path).write_text(render(result, cfg), encoding="utf-8")
    print(f"[bggwatch] analysis written to {path}: {len(result['ranked'])} variants scored", flush=True)
    return 0
