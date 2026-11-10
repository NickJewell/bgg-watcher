import datetime as dt
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from bggwatch import bgg, pipeline, report
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
        self.assertEqual((code, len(seen)), (0, 4))
        snap = Store(client.cfg.data_dir).load_snapshot("2026-11-10")
        self.assertEqual((snap[7]["plays"], snap[7]["hot_rank"], snap[7]["owned"]), (42, 12, 400))
        md = report.build(client.cfg)
        self.assertIn("[Seven](https://boardgamegeek.com/boardgame/7)", md)

        # A second run the same day is served entirely from cache.
        again, seen2, _ = make_client(tmp, responder, min_delay=0, jitter=0)
        pipeline.run(again.cfg, today=dt.date(2026, 11, 10), client=again)
        self.assertEqual(len(seen2), 0)


if __name__ == "__main__":
    unittest.main()
