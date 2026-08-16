"""Give an existing database the occupancy and alerts its real runs never got.

Any run imported through v0.1.1 has empty occupancy and empty alerts, because
edge/app.py imported neither module -- the only code that ever wrote those
tables was tools/seed_demo.py. This recomputes both from the assignments
already on disk.

    python -m tools.backfill_intelligence --reserve PENCH-MH
    python -m tools.backfill_intelligence --all

Oldest run first, and that order is not cosmetic: Stage 5 reads occupancy
HISTORY, so recomputing cycle 3 before cycle 2 compares against stale rows.

Safe to re-run. Both stages are pure functions of data already stored, and an
acknowledged alert keeps its acknowledgement (repo.replace_alerts()).
"""
from __future__ import annotations

import argparse
import sys

from edge.db import repo, repo_ext
from edge.pipeline import postprocess


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reserve", help="reserve_id; omit with --all")
    ap.add_argument("--all", action="store_true", help="every reserve")
    ap.add_argument("--actor", default="system")
    args = ap.parse_args()

    repo.migrate()
    if args.all:
        targets = [r["reserve_id"] for r in repo.reserves()]
    elif args.reserve:
        targets = [args.reserve]
    else:
        ap.error("pass --reserve <id> or --all")
        return 2

    total_occ = total_alerts = 0
    for rid in targets:
        if not repo.reserve(rid):
            print(f"  ! unknown reserve {rid}")
            continue
        print(f"\n{rid}")
        for out in postprocess.recompute_for_reserve(rid, actor=args.actor):
            if "error" in out:
                print(f"  ! {out['run_id']}: {out['error']}")
                continue
            o, a = out["occupancy"], out["alerts"]
            total_occ += o["individuals"]
            total_alerts += a["total"]
            print(f"  {out['run_id']}: {o['individuals']} occupancy rows "
                  f"({o['with_hull']} with a home-range polygon), "
                  f"{a['raised']} alerts raised, {a['suppressed']} suppressed")
            print(f"      {out['explanation']}")

    repo_ext.checkpoint_wal()
    print(f"\n{total_occ} occupancy rows, {total_alerts} alerts written.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
