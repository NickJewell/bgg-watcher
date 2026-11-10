"""Endpoint wrappers and parsers. No state, no scheduling."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

from .client import PoliteClient

POOL_PAGE_SIZE = 50  # server-side maximum
THING_BATCH = 20


def _num(value, cast=float, default=0):
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


# -- mechanic listing (undocumented JSON endpoint used by BGG's own site) ------
def parse_pool_page(body: bytes) -> tuple[list[dict], int]:
    data = json.loads(body)
    rows = []
    for it in data.get("items", []):
        rows.append(
            {
                "id": int(it["objectid"]),
                "name": it.get("name", ""),
                "year": _num(it.get("yearpublished"), int),
                "weight": round(_num(it.get("avgweight")), 3),
                "usersrated": _num(it.get("usersrated"), int),
                "average": round(_num(it.get("average")), 3),
                "owned": _num(it.get("numowned"), int),
                "wishing": _num(it.get("numwish"), int),
                "wanting": _num(it.get("numwanting"), int),
                "numcomments": _num(it.get("numcomments"), int),
                "rank": _num(it.get("rank"), int),
            }
        )
    return rows, _num(data.get("config", {}).get("numitems"), int)


def fetch_pool_page(client: PoliteClient, mechanic_id: int, page: int) -> tuple[list[dict], int]:
    body = client.get(
        "/api/geekitem/linkeditems",
        {
            "ajax": 1,
            "linkdata_index": "boardgame",
            "nosession": 1,
            "objectid": mechanic_id,
            "objecttype": "property",
            "subtype": "boardgamemechanic",
            "pageid": page,
            "showcount": POOL_PAGE_SIZE,
            "sort": "name",
        },
    )
    return parse_pool_page(body)


# -- thing (official XML API) --------------------------------------------------
def parse_things(body: bytes) -> list[dict]:
    out = []
    for item in ET.fromstring(body).findall("item"):
        def val(tag, cast=int):
            el = item.find(tag)
            return _num(el.get("value") if el is not None else None, cast)

        def links(kind):
            return [l.get("value") for l in item.findall(f"link[@type='{kind}']")]

        name = item.find("name[@type='primary']")
        ratings = item.find("statistics/ratings")

        def stat(tag, cast=int):
            el = ratings.find(tag) if ratings is not None else None
            return _num(el.get("value") if el is not None else None, cast)

        rank_el = ratings.find("ranks/rank[@name='boardgame']") if ratings is not None else None
        out.append(
            {
                "id": int(item.get("id")),
                "type": item.get("type"),
                "name": name.get("value") if name is not None else "",
                "year": val("yearpublished"),
                "min_players": val("minplayers"),
                "max_players": val("maxplayers"),
                "playing_time": val("playingtime"),
                "mechanics": links("boardgamemechanic"),
                "categories": links("boardgamecategory"),
                "designers": links("boardgamedesigner"),
                "publishers": links("boardgamepublisher")[:3],
                "usersrated": stat("usersrated"),
                "average": round(stat("average", float), 3),
                "rank": _num(rank_el.get("value") if rank_el is not None else None, int),
                "owned": stat("owned"),
                "wishing": stat("wishing"),
                "wanting": stat("wanting"),
                "numcomments": stat("numcomments"),
                "numweights": stat("numweights"),
                "weight": round(stat("averageweight", float), 3),
            }
        )
    return out


def fetch_things(client: PoliteClient, ids: list[int]) -> list[dict]:
    assert len(ids) <= THING_BATCH
    body = client.get("/xmlapi2/thing", {"id": ",".join(map(str, ids)), "stats": 1})
    return parse_things(body)


# -- hotness (official XML API) ------------------------------------------------
def parse_hot(body: bytes) -> dict[int, int]:
    return {int(i.get("id")): int(i.get("rank")) for i in ET.fromstring(body).findall("item")}


def fetch_hot(client: PoliteClient) -> dict[int, int]:
    return parse_hot(client.get("/xmlapi2/hot", {"type": "boardgame"}))


# -- plays (official XML API) --------------------------------------------------
def parse_plays_total(body: bytes) -> int:
    return _num(ET.fromstring(body).get("total"), int)


def fetch_plays_total(client: PoliteClient, game_id: int, mindate: str, maxdate: str) -> int:
    """Logged plays in the window. Only page 1 is requested: the count is in
    the root element, so the individual play records are never paged through."""
    body = client.get("/xmlapi2/plays", {"id": game_id, "mindate": mindate, "maxdate": maxdate})
    return parse_plays_total(body)
