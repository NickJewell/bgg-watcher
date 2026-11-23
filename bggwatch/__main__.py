import argparse
import sys

from . import analysis, pipeline, report
from .config import Config


def main() -> int:
    p = argparse.ArgumentParser(prog="bggwatch")
    p.add_argument("command", choices=["run", "report", "analyse", "cost"],
                   help="run: fetch today's snapshot and rewrite WATCHLIST.md; "
                        "report: rebuild WATCHLIST.md offline; "
                        "analyse STEP: scaffolding for the design analysis (see ANALYSIS_TASK.md); "
                        "cost: estimate requests per run from stored data, no network")
    p.add_argument("step", nargs="?", choices=list(analysis.STEPS), help="analysis step")
    p.add_argument("--budget", type=int, help="override the per-run BGG request budget")
    args = p.parse_args()
    cfg = Config.from_env()
    if args.budget is not None:
        cfg.request_budget = args.budget
    if args.command == "analyse":
        if not args.step:
            p.error("analyse needs a step: " + ", ".join(analysis.STEPS))
        return analysis.run(cfg, args.step)
    if args.command == "cost":
        from .store import Store
        print(pipeline.estimate_cost(cfg, Store(cfg.data_dir)))
        return 0
    code = pipeline.run(cfg) if args.command == "run" else 0
    report.write(cfg)
    return code


if __name__ == "__main__":
    sys.exit(main())
