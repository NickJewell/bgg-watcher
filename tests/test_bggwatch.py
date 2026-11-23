import datetime as dt
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from bggwatch import analysis, bgg, pipeline, report
from bggwatch.client import BudgetExceeded, PoliteClient, TooManyFailures
from bggwatch.config import Config
from bggwatch.store import Store


class FakeResp:
    def __init__(self, body, status=200):
        self._body, self.status = body, status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_client(tmp, responder, **over):
    cfg = Config(cache_dir=Path(tmp) / "cache", data_dir=Path(tmp) / "data", **over)
    seen, sleeps = [], []

    def opener(req, timeout=None):
        seen.append(req)
        return responder(req)

    return PoliteClient(cfg, opener=opener, sleep=sleeps.append), seen, sleeps


def http_error(code, headers=None):
    return urllib.error.HTTPError("u", code, "x", headers or {}, io.BytesIO(b""))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_budget_is_a_hard_cap(self):
        c, seen, _ = make_client(self.tmp, lambda r: FakeResp(b"ok"), request_budget=2)
        c.get("/a"), c.get("/b")
        with self.assertRaises(BudgetExceeded):
            c.get("/c")
        self.assertEqual(len(seen), 2)

    def test_cache_avoids_second_request(self):
        c, seen, _ = make_client(self.tmp, lambda r: FakeResp(b"ok"))
        self.assertEqual(c.get("/a", {"x": 1}), c.get("/a", {"x": 1}))
        self.assertEqual((len(seen), c.cache_hits), (1, 1))

    def test_requests_are_spaced(self):
        c, _, sleeps = make_client(self.tmp, lambda r: FakeResp(b"ok"))
        c._clock = lambda: 0.0  # no time passes between requests
        c.get("/a"), c.get("/b")
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], c.cfg.min_delay)

    def test_backs_off_on_202_and_429_then_succeeds(self):
        replies = iter([FakeResp(b"", 202), http_error(429, {"Retry-After": "90"}), FakeResp(b"ok")])

        def responder(req):
            r = next(replies)
            if isinstance(r, Exception):
                raise r
            return r

        c, seen, sleeps = make_client(self.tmp, responder, min_delay=0, jitter=0)
        self.assertEqual(c.get("/a"), b"ok")
        self.assertEqual(len(seen), 3)
        self.assertIn(90, sleeps)

    def test_4xx_is_not_retried_and_failures_abort_the_run(self):
        def responder(req):
            raise http_error(403)

        c, seen, _ = make_client(self.tmp, responder, max_failures=2, min_delay=0, jitter=0)
        with self.assertRaises(RuntimeError):
            c.get("/a")
        self.assertEqual(len(seen), 1)
        with self.assertRaises(TooManyFailures):
            c.get("/b")

    def test_token_comes_from_env_and_is_never_cached(self):
        import os
        os.environ["BGG_API_TOKEN"] = "s3cret-token"
        try:
            c, seen, _ = make_client(self.tmp, lambda r: FakeResp(b"ok"))
            c.get("/a")
        finally:
            del os.environ["BGG_API_TOKEN"]
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer s3cret-token")
        for f in Path(self.tmp).rglob("*"):
            if f.is_file():
                self.assertNotIn(b"s3cret", f.read_bytes())
                self.assertNotIn("s3cret", f.name)


THING_XML = b"""<items><item type="boardgame" id="7">
<name type="primary" value="Seven"/><name type="alternate" value="Sept"/>
<description>Win &amp;amp; lose&amp;#10;&amp;#10;on   purpose.</description>
<yearpublished value="2025"/><minplayers value="3"/><maxplayers value="5"/><playingtime value="20"/>
<link type="boardgamemechanic" id="2009" value="Trick-taking"/>
<link type="boardgamemechanic" id="2004" value="Set Collection"/>
<link type="boardgamedesigner" id="1" value="A. Designer"/>
<statistics><ratings><usersrated value="100"/><average value="7.5"/>
<ranks><rank type="subtype" name="boardgame" value="Not Ranked"/></ranks>
<owned value="400"/><wanting value="5"/><wishing value="60"/><numcomments value="9"/>
<numweights value="12"/><averageweight value="1.75"/></ratings></statistics></item></items>"""


class ParserTests(unittest.TestCase):
    def test_thing(self):
        (g,) = bgg.parse_things(THING_XML)
        self.assertEqual((g["id"], g["name"], g["min_players"], g["max_players"]), (7, "Seven", 3, 5))
        self.assertEqual((g["weight"], g["numweights"], g["owned"], g["rank"]), (1.75, 12, 400, 0))
        self.assertEqual(g["mechanics"], ["Trick-taking", "Set Collection"])
        self.assertEqual(g["description"], "Win & lose on purpose.")

    def test_pool_page(self):
        body = json.dumps({"items": [{"objectid": "7", "name": "Seven", "yearpublished": "2025",
                                      "avgweight": "1.7500", "numowned": "400", "numwish": "60"}],
                           "config": {"numitems": 3332}}).encode()
        rows, total = bgg.parse_pool_page(body)
        self.assertEqual((rows[0]["id"], rows[0]["weight"], rows[0]["owned"], total), (7, 1.75, 400, 3332))

    def test_plays_and_hot(self):
        self.assertEqual(bgg.parse_plays_total(b'<plays total="474" page="1"><play id="1"/></plays>'), 474)
        self.assertEqual(bgg.parse_hot(b'<items><item id="9" rank="3"/></items>'), {9: 3})


class DecisionTests(unittest.TestCase):
    cfg = Config()

    def game(self, **kw):
        return {"id": 1, "type": "boardgame", "min_players": 3, "max_players": 5, "weight": 1.8,
                "numweights": 20, "year": 2020, "owned": 100, "wishing": 10, **kw}

    def test_classify(self):
        self.assertEqual(pipeline.classify(self.game(), self.cfg), "confirmed")
        self.assertEqual(pipeline.classify(self.game(weight=2.4), self.cfg), "out")
        self.assertEqual(pipeline.classify(self.game(numweights=2, weight=3.0), self.cfg), "unconfirmed")
        self.assertEqual(pipeline.classify(self.game(min_players=5, max_players=8), self.cfg), "out")
        self.assertEqual(pipeline.classify(self.game(min_players=2, max_players=2), self.cfg), "confirmed")
        self.assertEqual(pipeline.classify(self.game(type="boardgameexpansion"), self.cfg), "out")

    def test_candidate_prefilter(self):
        row = lambda w, y: {"weight": w, "year": y}
        self.assertTrue(pipeline.is_candidate(row(1.5, 1990), self.cfg, 2026))
        self.assertFalse(pipeline.is_candidate(row(0, 1990), self.cfg, 2026))
        self.assertTrue(pipeline.is_candidate(row(0, 2026), self.cfg, 2026))
        self.assertTrue(pipeline.is_candidate(row(2.3, 2025), self.cfg, 2026))
        self.assertFalse(pipeline.is_candidate(row(3.5, 2026), self.cfg, 2026))

    def test_other_families_must_be_card_games_and_get_their_own_quota(self):
        ladder = lambda **kw: self.game(mechanics=["Ladder Climbing"], **kw)
        self.assertEqual(pipeline.classify(ladder(categories=["Card Game"]), self.cfg), "confirmed")
        self.assertEqual(pipeline.classify(ladder(categories=["Dice"]), self.cfg), "out")
        self.assertEqual(pipeline.classify(self.game(mechanics=["Trick-taking", "Ladder Climbing"],
                                                     categories=["Dice"]), self.cfg), "confirmed")
        games = {i: {**self.game(id=i, wishing=i), "status": "confirmed", "mechanics": ["Trick-taking"]}
                 for i in range(1, 6)}
        games |= {i: {**ladder(id=i, wishing=i), "status": "confirmed"} for i in range(6, 10)}
        cfg = Config(watchlist_size=2, family_watch_size=1)
        self.assertEqual(pipeline.select_watchlist(games, {}, cfg, 2026), [5, 4, 9])

        kept = {}
        thing = lambda i, cats: {**ladder(id=i, categories=cats), "name": "X", "description": "long",
                                 "mechanic_ids": {"Ladder Climbing": 2079, "Dice Rolling": 2072}}
        ids = {}
        pipeline.merge_details(kept, [thing(1, ["Card Game"]), thing(2, ["Dice"])], self.cfg, "2026-11-10", ids)
        self.assertEqual(ids, {"Ladder Climbing": 2079})  # only configured families are remembered
        self.assertEqual(kept[1]["description"], "long")
        self.assertEqual(kept[2], {"id": 2, "name": "X", "type": "boardgame", "year": 2020, "status": "out",
                                   "first_seen": "2026-11-10", "details_at": "2026-11-10"})

    def test_a_long_listing_is_read_over_several_runs(self):
        tmp = tempfile.mkdtemp()
        page = lambda n: json.dumps({"items": [{"objectid": str(n), "name": f"G{n}"}],
                                     "config": {"numitems": 150}}).encode()
        client, seen, _ = make_client(tmp, lambda r: FakeResp(page(len(seen))), min_delay=0, jitter=0,
                                      pool_pages_per_run=2)
        store, state = Store(client.cfg.data_dir), {}
        args = (client, store, client.cfg, state, "Ladder Climbing", 2079)
        self.assertFalse(pipeline.refresh_family_pool(*args, "2026-11-10"))
        self.assertEqual(state["pools"]["Ladder Climbing"], {"size": 150, "next_page": 3})
        self.assertEqual(store.load_pool("Ladder Climbing"), [])  # nothing is published until it is complete
        self.assertTrue(pipeline.refresh_family_pool(*args, "2026-11-11"))
        self.assertEqual(state["pools"]["Ladder Climbing"], {"size": 150, "refreshed": "2026-11-11"})
        self.assertEqual([r["id"] for r in store.load_pool("Ladder Climbing")], [1, 2, 3])
        self.assertEqual((len(seen), store.pool_path("Ladder Climbing", partial=True).exists()), (3, False))

    def test_watchlist_is_capped_and_hot_games_come_first(self):
        games = {i: {**self.game(id=i, wishing=i), "status": "confirmed"} for i in range(1, 11)}
        games[10]["status"] = "out"
        cfg = Config(watchlist_size=3)
        self.assertEqual(pipeline.select_watchlist(games, {2: 40}, cfg, 2026), [2, 9, 8])


class ReportTests(unittest.TestCase):
    def test_baseline_never_overlaps_latest_window(self):
        dates = ["2026-11-01", "2026-11-03", "2026-11-09", "2026-11-10"]
        self.assertEqual(report.baseline_date(dates, "2026-11-10", 7), "2026-11-03")
        self.assertIsNone(report.baseline_date(dates[2:], "2026-11-10", 7))

    def test_heat_orders_by_activity(self):
        row = lambda p, o, w, h=None: {"plays": p, "owned": o, "wishing": w, "hot_rank": h}
        heat = report.heat_scores({1: row(100, 500, 300, 4), 2: row(10, 500, 30), 3: row(0, 500, 0)})
        self.assertTrue(heat[1] > heat[2] > heat[3])
        self.assertEqual(heat[1], 100.0)

    def test_mechanic_lift(self):
        g = lambda y, m: {"status": "confirmed", "year": y, "mechanics": ["Trick-taking", *m]}
        games = dict(enumerate([g(2025, ["Co-op"])] * 3 + [g(2026, [])] + [g(2010, ["Co-op"])] + [g(2011, [])] * 7))
        rows, n_recent, n_older = report.mechanic_trends(games, 2026, "Trick-taking")
        self.assertEqual((n_recent, n_older), (4, 8))
        self.assertEqual(rows[0][0], "Co-op")
        self.assertAlmostEqual(rows[0][4], 6.0)


class EndToEndTests(unittest.TestCase):
    def test_run_snapshots_reports_and_stays_in_budget(self):
        tmp = tempfile.mkdtemp()
        pool = json.dumps({"items": [{"objectid": "7", "name": "Seven", "yearpublished": "2025",
                                      "avgweight": "1.75", "numowned": "400", "numwish": "60"}],
                           "config": {"numitems": 1}}).encode()

        def responder(req):
            url = req.full_url
            if "/hot" in url:
                return FakeResp(b'<items><item id="7" rank="12"/></items>')
            if "linkeditems" in url:
                return FakeResp(pool)
            if "/thing" in url:
                return FakeResp(THING_XML)
            self.assertIn("mindate=2026-11-03&maxdate=2026-11-09", url)
            return FakeResp(b'<plays total="42"/>')

        client, seen, _ = make_client(tmp, responder, min_delay=0, jitter=0)
        code = pipeline.run(client.cfg, today=dt.date(2026, 11, 10), client=client)
        self.assertEqual((code, len(seen)), (0, 5))  # hot, listing, details, plays, then the Set Collection listing
        store = Store(client.cfg.data_dir)
        self.assertEqual(store.load_state()["mechanic_ids"], {"Trick-taking": 2009, "Set Collection": 2004})
        self.assertIn("objectid=2004", seen[-1].full_url)
        self.assertEqual([r["id"] for r in store.load_pool("Set Collection")], [7])
        self.assertEqual(store.load_state()["pools"]["Set Collection"], {"refreshed": "2026-11-10", "size": 1})
        snap = Store(client.cfg.data_dir).load_snapshot("2026-11-10")
        self.assertEqual((snap[7]["plays"], snap[7]["hot_rank"], snap[7]["owned"]), (42, 12, 400))
        md = report.build(client.cfg)
        self.assertIn("[Seven](https://boardgamegeek.com/boardgame/7)", md)

        # A failing listing for another family is logged and skipped; the snapshot is still written.
        tmp2 = tempfile.mkdtemp()

        def flaky(req):
            if "objectid=2004" in req.full_url:
                raise http_error(404)
            return responder(req)

        broken, _, _ = make_client(tmp2, flaky, min_delay=0, jitter=0)
        self.assertEqual(pipeline.run(broken.cfg, today=dt.date(2026, 11, 10), client=broken), 0)
        self.assertEqual(Store(broken.cfg.data_dir).load_snapshot("2026-11-10")[7]["plays"], 42)

        # A second run the same day is served entirely from cache.
        again, seen2, _ = make_client(tmp, responder, min_delay=0, jitter=0)
        pipeline.run(again.cfg, today=dt.date(2026, 11, 10), client=again)
        self.assertEqual(len(seen2), 0)


class AnalysisTests(unittest.TestCase):
    def variant(self, n, **kw):
        return {"title": f"Variant {n}", "family": "Trick-taking", "hook": "h", "core_twist": f"twist {n}", "theme": "t",
                "remixes": ["Seven"], "mechanics": ["Trick-taking"], "players_min": 2, "players_max": 4,
                "est_weight": 1.8, **kw}

    def scores(self, i, value):
        return {"id": i, **{k: value for k in analysis.RUBRIC}, "rationale": f"r{i}"}

    def write(self, name, obj):
        (analysis.WORK / name).write_text(json.dumps(obj))

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config(data_dir=Path(self.tmp), analysis_variants=10, analysis_keep=3)
        self._work = analysis.WORK
        analysis.WORK = Path(self.tmp) / "work"
        self.addCleanup(setattr, analysis, "WORK", self._work)
        self._archive = analysis.ARCHIVE
        analysis.ARCHIVE = Path(self.tmp) / "analyses"
        self.addCleanup(setattr, analysis, "ARCHIVE", self._archive)
        store = Store(self.tmp)
        store.save_games({7: bgg.parse_things(THING_XML)[0], 8: {"min_players": 2, "max_players": 4}})
        store.save_snapshot("2026-11-10", [
            {"id": 7, "name": "Seven", "plays": 42, "hot_rank": None, "owned": 400, "wishing": 60},
            {"id": 8, "name": "Eight", "plays": 1, "hot_rank": 9, "owned": 400, "wishing": 0}])
        sig = [{"name": "Must-not-follow", "seen_in": ["Seven"], "why_it_matters": "x"}]
        self.generated = {"mechanics": sig, "themes": sig, "differentiators": sig,
                          "variants": [self.variant(i) for i in range(1, 11)]}

    def test_total_is_weighted_and_clamped(self):
        self.assertEqual(analysis.total_score(self.scores(1, 10)), 100.0)
        self.assertEqual(analysis.total_score(self.scores(1, 99)), 100.0)
        self.assertEqual(analysis.total_score(self.scores(1, 0)), 10.0)
        s = {**self.scores(1, 5), "mainstream_reach": 10}
        self.assertEqual(analysis.total_score(s), 65.0)
        self.assertEqual(analysis.framework_scores(s), {"Koster": 5.0, "MDA": 5.0, "Market": 10.0})

    def test_sources_take_top_heat_plus_hot_list(self):
        self.cfg.analysis_sources = 1
        date, src = analysis.select_sources(self.cfg, Store(self.tmp))
        self.assertEqual((date, [s["name"] for s in src]), ("2026-11-10", ["Seven", "Eight"]))
        self.assertEqual(src[0]["description"], "Win & lose on purpose.")

    def test_check_rejects_variants_that_break_the_constraints(self):
        analysis.step_brief(self.cfg)
        bad = [self.variant(1, players_max=5), self.variant(2, players_min=1), self.variant(3, est_weight=2.4),
               self.variant(4), self.variant(4)]
        self.write("generated.json", {**self.generated, "variants": bad + self.generated["variants"][4:]})
        with self.assertRaises(analysis.AnalysisError) as ctx:
            analysis.step_check(self.cfg)
        msg = str(ctx.exception)
        self.assertIn("7 valid variants, 3 more needed", msg)
        for why in ("player range 2-5", "player range 1-4", "weight 2.4", "duplicate title"):
            self.assertIn(why, msg)
        self.assertFalse((analysis.WORK / "variants.json").exists())

    def test_wrong_shape_is_reported(self):
        analysis.step_brief(self.cfg)
        broken = self.variant(1)
        del broken["hook"]
        broken["players_min"] = "two"
        self.write("generated.json", {**self.generated, "variants": [broken]})
        with self.assertRaises(analysis.AnalysisError) as ctx:
            analysis.step_check(self.cfg)
        self.assertIn("$.variants[0]: missing 'hook'", str(ctx.exception))
        self.assertIn("$.variants[0].players_min: expected integer", str(ctx.exception))

    def test_full_flow_ranks_in_code_and_renders(self):
        self.assertIn("brief.md", analysis.step_brief(self.cfg))
        brief = (analysis.WORK / "brief.md").read_text()
        self.assertIn("Seven", brief)
        self.assertIn("10 novel game variants", brief)
        self.write("generated.json", self.generated)
        analysis.step_check(self.cfg)
        self.assertNotIn("remixes", (analysis.WORK / "score-brief.md").read_text().split("<pitches>")[1])

        self.write("scores.json", {"scores": [self.scores(i, i) for i in range(1, 10)]})
        with self.assertRaises(analysis.AnalysisError) as ctx:
            analysis.step_rank(self.cfg)
        self.assertIn("no score for ids [10]", str(ctx.exception))
        self.write("scores.json", {"scores": [self.scores(i, i) for i in range(1, 11)]})
        self.assertIn("100.0  Variant 10", analysis.step_rank(self.cfg))
        self.assertIn('"id": 8', (analysis.WORK / "pitch-brief.md").read_text())

        out = Path(self.tmp) / "A.md"
        out.write_text("old")
        self.write("pitches.json", {"pitches": [{"id": i, "pitch": "One paragraph only."} for i in (10, 9, 8)]})
        with self.assertRaises(analysis.AnalysisError):
            analysis.step_render(self.cfg, str(out))
        self.assertEqual(out.read_text(), "old")  # a failed run never clobbers the last good file
        self.assertFalse(analysis.ARCHIVE.exists())

        self.write("pitches.json", {"pitches": [{"id": i, "pitch": f"Pitch {i}.\n\nSecond paragraph."} for i in (10, 9, 8)]})
        analysis.step_render(self.cfg, str(out))
        md = out.read_text()
        self.assertIn("### 1. Variant 10 — 100.0/100", md)
        self.assertIn("### 3. Variant 8 — 80.0/100", md)
        self.assertNotIn("### 4.", md)
        self.assertIn("Pitch 10.\n\nSecond paragraph.", md)
        self.assertIn("## All 10 variants", md)
        dated = list(analysis.ARCHIVE.glob("*.md"))
        self.assertEqual([d.name for d in dated], [Store(self.tmp).snapshot_dates()[-1] + ".md"])
        self.assertEqual(dated[0].read_text(), md)  # the dated copy is kept when the next run overwrites A.md
        (entry,) = analysis.load_history()
        self.assertEqual((entry["date"], [s["id"] for s in entry["sources"]]), ("2026-11-10", [7, 8]))
        self.assertEqual((entry["variants"][0]["title"], entry["variants"][0]["rank"]), ("Variant 10", 1))

    def history(self, date="2026-11-03", sources=(7,), variants=()):
        analysis.ARCHIVE.mkdir(exist_ok=True)
        (analysis.ARCHIVE / "index.json").write_text(json.dumps({"analyses": [{
            "date": date, "sources": [{"id": i, "name": str(i), "family": "Trick-taking"} for i in sources],
            "variants": [{"rank": 1, "family": "Trick-taking", "hook": "h", "total": 50.0, **v} for v in variants]}]}))

    def test_a_slot_is_held_for_a_game_that_was_not_a_source_last_week(self):
        store = Store(self.tmp)
        row = lambda i, plays: {"id": i, "name": f"G{i}", "plays": plays, "hot_rank": None, "owned": 400, "wishing": 0}
        store.save_snapshot("2026-11-17", [row(1, 90), row(2, 80), row(3, 70), row(4, 5)])
        self.cfg.analysis_sources = 3
        names = lambda prev: [s["name"] for s in analysis.select_sources(self.cfg, store, prev)[1]]
        self.assertEqual(names(None), ["G1", "G2", "G3"])
        self.assertEqual(names({1, 2, 3}), ["G1", "G2", "G4"])  # the weakest newcomer displaces the weakest regular
        src = analysis.select_sources(self.cfg, store, {1, 2, 3})[1]
        self.assertEqual([s["new_this_week"] for s in src], [False, False, True])

    def test_sources_are_shared_between_families_and_heat_is_per_family(self):
        store = Store(self.tmp)
        tt = {"mechanics": ["Trick-taking"]}
        lc = {"mechanics": ["Ladder Climbing"], "categories": ["Card Game"]}
        store.save_games({1: tt, 2: tt, 3: tt, 4: lc, 5: lc})
        row = lambda i, plays: {"id": i, "name": f"G{i}", "plays": plays, "hot_rank": None, "owned": 400, "wishing": 0}
        store.save_snapshot("2026-11-17", [row(1, 900), row(2, 800), row(3, 700), row(4, 9), row(5, 8)])
        self.cfg.analysis_sources = 4
        src = analysis.select_sources(self.cfg, store)[1]
        self.assertEqual([(s["name"], s["family"]) for s in src],
                         [("G1", "Trick-taking"), ("G2", "Trick-taking"), ("G4", "Ladder Climbing"), ("G5", "Ladder Climbing")])
        self.assertEqual(src[2]["heat"], src[0]["heat"])  # top of its own family, however few plays

        analysis.step_brief(self.cfg)
        brief = (analysis.WORK / "brief.md").read_text()
        self.assertIn("at least 3 for each family, at least 1 hybrids", brief)
        mixed = [self.variant(i, family=f) for i, f in enumerate(
            ["Trick-taking"] * 5 + ["Ladder Climbing"] * 2 + ["Hybrid"] * 3, 1)]
        self.write("generated.json", {**self.generated, "variants": mixed})
        with self.assertRaises(analysis.AnalysisError) as ctx:
            analysis.step_check(self.cfg)
        self.assertIn("Ladder Climbing: 2, needs at least 3", str(ctx.exception))
        mixed[4]["family"] = "Ladder Climbing"
        mixed[9]["family"] = "Solitaire"
        self.write("generated.json", {**self.generated, "variants": mixed})
        with self.assertRaises(analysis.AnalysisError) as ctx:
            analysis.step_check(self.cfg)
        self.assertIn("family 'Solitaire' is not one of: Trick-taking, Ladder Climbing, Hybrid", str(ctx.exception))
        mixed[9]["family"] = "Hybrid"
        self.write("generated.json", {**self.generated, "variants": mixed})
        self.assertIn("4 Trick-taking, 3 Ladder Climbing, 3 Hybrid", analysis.step_check(self.cfg))

        # Scores rise with id, so the hybrids (8-10) and ladder games (5-7) outscore trick-taking (1-4).
        self.cfg.analysis_keep, self.cfg.analysis_family_cap = 5, 2
        self.write("scores.json", {"scores": [self.scores(i, i) for i in range(1, 11)]})
        out = analysis.step_rank(self.cfg)
        self.assertEqual(json.loads((analysis.WORK / "top.json").read_text()), [10, 9, 7, 6, 4])
        self.assertIn("passed over by the cap of 2 per family", out)

    def test_top_places_are_capped_per_family_and_filled_if_short(self):
        v = lambda i, fam, total: {"id": i, "family": fam, "total": total}
        ranked = [v(1, "A", 90), v(2, "A", 80), v(3, "A", 70), v(4, "B", 60), v(5, "A", 50)]
        self.assertEqual([x["id"] for x in analysis.pick_top(ranked, 3, 2)], [1, 2, 4])
        self.assertEqual([x["id"] for x in analysis.pick_top(ranked, 4, 1)], [1, 2, 3, 4])  # cap cannot be met

    def test_earlier_weeks_are_remembered(self):
        self.history(variants=[{"title": "Old Hat", "core_twist": "The second-highest card of the led suit wins the trick."}])
        analysis.step_brief(self.cfg)
        self.assertIn("- Old Hat: h", (analysis.WORK / "brief.md").read_text())
        repeat = [self.variant(1, title="old hat"),
                  self.variant(2, core_twist="The second-highest card of the suit led wins every trick.")]
        self.write("generated.json", {**self.generated, "variants": repeat + self.generated["variants"][2:]})
        with self.assertRaises(analysis.AnalysisError) as ctx:
            analysis.step_check(self.cfg)
        self.assertIn("'old hat': title already used on 2026-11-03", str(ctx.exception))
        self.assertIn("'Variant 2': core twist repeats 'Old Hat' from 2026-11-03", str(ctx.exception))

    def test_steps_fail_cleanly_out_of_order(self):
        self.assertEqual(analysis.run(self.cfg, "rank"), 1)
        self.assertEqual(analysis.run(self.cfg, "render"), 1)


if __name__ == "__main__":
    unittest.main()
