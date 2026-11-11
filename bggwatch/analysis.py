"""Ludological analysis of the latest snapshot, written to ANALYSIS_SNAPSHOT.md.

The creative work is done by a Claude Code session (see ANALYSIS_TASK.md); this
module is the deterministic scaffolding around it. It makes no network requests
and calls no API. The session alternates with these steps:

  brief   write the generation brief from the latest snapshot
          -> the session writes .analysis/generated.json
  check   validate the variants, assign ids, write the scoring brief
          -> a fresh sub-agent writes .analysis/scores.json
  rank    compute weighted totals, keep the top K, write the pitch brief
          -> the session writes .analysis/pitches.json
  render  write ANALYSIS_SNAPSHOT.md

Constraints, de-duplication, totals and the top-K cut are enforced here, so the
model cannot quietly bend them.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

from .config import Config
from .report import heat_scores
from .store import Store

OUTPUT = "ANALYSIS_SNAPSHOT.md"
WORK = Path(".analysis")

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
SCORE_SCHEMA = _obj({"scores": {"type": "array", "items": _obj(
    {"id": _INT, **{k: _INT for k in RUBRIC}, "rationale": _STR})}})
PITCH_SCHEMA = _obj({"pitches": {"type": "array", "items": _obj({"id": _INT, "pitch": _STR})}})

ROLE = (
    "Approach this as a tabletop game designer and ludologist who specialises in trick-taking card "
    "games, who knows the modern trick-taking scene well, from traditional games to the current wave of "
    "small-box Japanese and Western designs, and who reasons about games through Raph Koster's "
    "Theory of Fun (fun is the pleasure of learning and mastering patterns; a game dies when its "
    "pattern is either solved or illegible) and the MDA framework (designers set Mechanics, which "
    "give rise to Dynamics at the table, which players experience as Aesthetics). "
    "The reader is a designer deciding what to prototype next, so be concrete and specific: name "
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


# -- pure steps (unit tested) ---------------------------------------------------
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


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render(result: dict, cfg: Config) -> str:
    top, ranked = result["top"], result["ranked"]
    L = [f"# Trick-taking design analysis — {result['date']}", "",
         f"Generated by a scheduled Claude Code session from the {len(result['sources'])} most active games in the "
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



# -- validation of what the session wrote -----------------------------------------
def schema_errors(value, schema: dict, path: str = "$") -> list[str]:
    """Check a value against the small JSON-schema subset used above."""
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict):
            return [f"{path}: expected an object"]
        errs = [f"{path}: missing '{k}'" for k in schema["required"] if k not in value]
        for k, sub in schema["properties"].items():
            if k in value:
                errs += schema_errors(value[k], sub, f"{path}.{k}")
        return errs
    if kind == "array":
        if not isinstance(value, list):
            return [f"{path}: expected an array"]
        return [e for i, v in enumerate(value) for e in schema_errors(v, schema["items"], f"{path}[{i}]")]
    ok = {"string": lambda v: isinstance(v, str),
          "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
          "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)}[kind](value)
    return [] if ok else [f"{path}: expected {kind}"]


def _load(name: str, schema: dict) -> dict:
    path = WORK / name
    if not path.exists():
        raise AnalysisError(f"{path} does not exist yet")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise AnalysisError(f"{path} is not valid JSON: {e}") from e
    errs = schema_errors(data, schema)
    if errs:
        raise AnalysisError(f"{path} has the wrong shape:\n  " + "\n  ".join(errs[:15]))
    return data


def _save(name: str, obj) -> None:
    WORK.mkdir(exist_ok=True)
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=1)
    (WORK / name).write_text(text + "\n", encoding="utf-8")


def _format(schema: dict) -> str:
    return ("Write the result as a single JSON document matching this JSON schema exactly "
            f"(every key required, no extras):\n\n```json\n{json.dumps(schema)}\n```")


# -- steps ------------------------------------------------------------------------
def step_brief(cfg: Config) -> str:
    date, sources = select_sources(cfg, Store(cfg.data_dir))
    shutil.rmtree(WORK, ignore_errors=True)
    _save("sources.json", {"date": date, "sources": sources})
    _save("brief.md", f"{ROLE}\n\n{generate_prompt(cfg, sources, cfg.analysis_variants)}\n\n"
                      f"{_format(GENERATE_SCHEMA)}\n\nSave it as `{WORK / 'generated.json'}`.")
    return f"Snapshot {date}, {len(sources)} source games. Read {WORK / 'brief.md'} and follow it."


def reject_reason(v: dict, cfg: Config, seen: set[str]) -> str | None:
    if v["title"].strip().lower() in seen:
        return "duplicate title"
    if not (cfg.min_players <= v["players_min"] <= v["players_max"] <= cfg.max_players):
        return f"player range {v['players_min']}-{v['players_max']} is outside {cfg.min_players}-{cfg.max_players}"
    if not (cfg.min_weight <= v["est_weight"] <= cfg.max_weight):
        return f"weight {v['est_weight']} is outside {cfg.min_weight}-{cfg.max_weight}"
    if not v["title"].strip() or not v["core_twist"].strip():
        return "empty title or core twist"
    return None


def step_check(cfg: Config) -> str:
    gen = _load("generated.json", GENERATE_SCHEMA)
    variants, seen, rejected = [], set(), []
    for v in gen["variants"]:
        why = reject_reason(v, cfg, seen)
        if why:
            rejected.append(f"'{v['title']}': {why}")
        else:
            seen.add(v["title"].strip().lower())
            variants.append({**v, "id": len(variants) + 1})
    need = cfg.analysis_variants - len(variants)
    if need > 0:
        raise AnalysisError(
            f"{len(variants)} valid variants, {need} more needed. Fix or replace these in "
            f"{WORK / 'generated.json'} and run check again:\n  " + "\n  ".join(rejected or ["(none rejected; the list is short)"]))
    variants = variants[: cfg.analysis_variants]
    _save("variants.json", variants)
    _save("score-brief.md", f"{ROLE}\n\n{score_prompt(variants)}\n\n{_format(SCORE_SCHEMA)}\n\n"
                            f"Save it as `{WORK / 'scores.json'}`.")
    return (f"{len(variants)} variants accepted. Have a fresh sub-agent, which has not seen how they were "
            f"written, read {WORK / 'score-brief.md'} and follow it.")


def step_rank(cfg: Config) -> str:
    variants = json.loads((WORK / "variants.json").read_text(encoding="utf-8"))
    scores = _load("scores.json", SCORE_SCHEMA)["scores"]
    missing = sorted({v["id"] for v in variants} - {s["id"] for s in scores})
    if missing:
        raise AnalysisError(f"{WORK / 'scores.json'} has no score for ids {missing}")
    ranked = rank(variants, scores)
    top = ranked[: cfg.analysis_keep]
    _save("ranked.json", ranked)
    brief = [{k: v[k] for k in ("id", "title", "hook", "core_twist", "theme", "remixes", "mechanics",
                                "players_min", "players_max", "est_weight", "total")}
             | {"rationale": v["scores"]["rationale"]} for v in top]
    _save("pitch-brief.md", f"{ROLE}\n\n{pitch_prompt(cfg, brief)}\n\n{_format(PITCH_SCHEMA)}\n\n"
                            f"Save it as `{WORK / 'pitches.json'}`.")
    lines = "\n".join(f"  {v['total']:5.1f}  {v['title']}" for v in top)
    return f"Top {len(top)} of {len(ranked)}:\n{lines}\nRead {WORK / 'pitch-brief.md'} and follow it."


def step_render(cfg: Config, path: str = OUTPUT) -> str:
    src = json.loads((WORK / "sources.json").read_text(encoding="utf-8"))
    gen = _load("generated.json", GENERATE_SCHEMA)
    ranked = json.loads((WORK / "ranked.json").read_text(encoding="utf-8"))
    pitches = {p["id"]: p["pitch"].strip() for p in _load("pitches.json", PITCH_SCHEMA)["pitches"]}
    top = ranked[: cfg.analysis_keep]
    for v in top:
        paragraphs = [p for p in pitches.get(v["id"], "").split("\n\n") if p.strip()]
        if not 2 <= len(paragraphs) <= 3:
            raise AnalysisError(f"the pitch for '{v['title']}' (id {v['id']}) has {len(paragraphs)} "
                                "paragraphs; it needs two or three, separated by blank lines")
        v["pitch"] = "\n\n".join(paragraphs)
    result = {"date": src["date"], "sources": src["sources"], "ranked": ranked, "top": top,
              "signals": {k: gen[k] for k in ("mechanics", "themes", "differentiators")}}
    Path(path).write_text(render(result, cfg), encoding="utf-8")
    return f"Wrote {path}: {len(ranked)} variants scored, top {len(top)} pitched."


STEPS = {"brief": step_brief, "check": step_check, "rank": step_rank, "render": step_render}


def run(cfg: Config, step: str) -> int:
    try:
        print(STEPS[step](cfg), flush=True)
        return 0
    except (AnalysisError, FileNotFoundError) as e:
        print(f"[bggwatch] analysis {step} failed: {e}", flush=True)
        return 1
