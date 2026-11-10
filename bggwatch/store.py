"""Flat-file storage: CSV and JSON so that history diffs cleanly in git."""

from __future__ import annotations

import csv
import json
from pathlib import Path

POOL_FIELDS = ["id", "name", "year", "weight", "usersrated", "average", "owned",
               "wishing", "wanting", "numcomments", "rank", "first_seen"]
SNAPSHOT_FIELDS = ["id", "name", "plays", "hot_rank", "owned", "wishing", "wanting",
                   "usersrated", "average", "numcomments", "rank"]
_FLOATS = {"weight", "average"}
_TEXT = {"name", "first_seen"}


def _coerce(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if k in _TEXT:
            out[k] = v
        elif v == "":
            out[k] = None
        else:
            out[k] = float(v) if k in _FLOATS else int(v)
    return out


class Store:
    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        self.snap_dir = self.dir / "snapshots"

    # -- generic -------------------------------------------------------------
    def _read_csv(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        with path.open(newline="", encoding="utf-8") as f:
            return [_coerce(r) for r in csv.DictReader(f)]

    def _write_csv(self, path: Path, fields: list[str], rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
            w.writeheader()
            for r in rows:
                w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})

    def _read_json(self, path: Path, default):
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    def _write_json(self, path: Path, obj) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
                        encoding="utf-8")

    # -- datasets ------------------------------------------------------------
    def load_pool(self) -> list[dict]:
        return self._read_csv(self.dir / "pool.csv")

    def save_pool(self, rows: list[dict]) -> None:
        self._write_csv(self.dir / "pool.csv", POOL_FIELDS, sorted(rows, key=lambda r: r["id"]))

    def load_games(self) -> dict[int, dict]:
        return {int(k): v for k, v in self._read_json(self.dir / "games.json", {}).items()}

    def save_games(self, games: dict[int, dict]) -> None:
        self._write_json(self.dir / "games.json", {str(k): v for k, v in games.items()})

    def load_state(self) -> dict:
        return self._read_json(self.dir / "state.json", {})

    def save_state(self, state: dict) -> None:
        self._write_json(self.dir / "state.json", state)

    def snapshot_dates(self) -> list[str]:
        return sorted(p.stem for p in self.snap_dir.glob("*.csv")) if self.snap_dir.exists() else []

    def load_snapshot(self, date: str) -> dict[int, dict]:
        return {r["id"]: r for r in self._read_csv(self.snap_dir / f"{date}.csv")}

    def save_snapshot(self, date: str, rows: list[dict]) -> None:
        self._write_csv(self.snap_dir / f"{date}.csv", SNAPSHOT_FIELDS, rows)
