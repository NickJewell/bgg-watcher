import argparse
import sys

from . import analysis, pipeline, report
from .config import Config


def main() -> int:
    p = argparse.ArgumentParser(prog="bggwatch")
    p.add_argument("command", choices=["run", "report", "analyse"],
                   help="run: fetch today's snapshot and rewrite WATCHLIST.md; "
                        "report: rebuild WATCHLIST.md offline; "
                        "analyse: write ANALYSIS_SNAPSHOT.md from the latest snapshot (uses the Claude API)")
    p.add_argument("--budget", type=int, help="override the per-run BGG request budget")
    args = p.parse_args()
    cfg = Config.from_env()
    if args.budget is not None:
        cfg.request_budget = args.budget
    if args.command == "analyse":
        return analysis.run(cfg)
    code = pipeline.run(cfg) if args.command == "run" else 0
    report.write(cfg)
    return code


if __name__ == "__main__":
    sys.exit(main())
