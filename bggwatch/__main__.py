import argparse
import sys

from . import pipeline, report
from .config import Config


def main() -> int:
    p = argparse.ArgumentParser(prog="bggwatch", description=__doc__)
    p.add_argument("command", choices=["run", "report"],
                   help="run: fetch today's snapshot then write the report; report: rebuild WATCHLIST.md offline")
    p.add_argument("--budget", type=int, help="override the per-run request budget")
    args = p.parse_args()
    cfg = Config.from_env()
    if args.budget is not None:
        cfg.request_budget = args.budget
    code = pipeline.run(cfg) if args.command == "run" else 0
    report.write(cfg)
    return code


if __name__ == "__main__":
    sys.exit(main())
