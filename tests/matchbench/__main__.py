"""python -m tests.matchbench [check|accept|import|refresh|drift|results] - see docs/match-testing.md."""

from __future__ import annotations

import argparse
import sys

from . import bench


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tests.matchbench", description=__doc__)
    sub = ap.add_subparsers(dest="cmd")
    check = sub.add_parser("check", help="run offline, print the scoreboard, exit 1 if the gate fails (default)")
    check.add_argument("-v", "--verbose", action="store_true", help="also list the declared-unlabelled rows")
    acc = sub.add_parser("accept", help="make this run the baseline; every changed row records --why")
    acc.add_argument("--why", required=True)
    acc.add_argument("--allow-regression", action="store_true",
                     help="accept a row that left the pool or stopped being right (the owner's call)")
    imp = sub.add_parser("import", help="merge a state database into corpus.csv (read-only on the DB)")
    imp.add_argument("--db", required=True)
    imp.add_argument("--drop-missing", action="store_true")
    sub.add_parser("refresh", help="fetch the lookups the frozen cache is missing (network; additions only)")
    dr = sub.add_parser("drift", help="re-fetch every frozen response live and report what would change")
    dr.add_argument("--out", default="", help="write the live catalog here (never over the frozen one)")
    sub.add_parser("calibrate", help="how closely the synthetic catalog model reproduces the frozen answers")
    res = sub.add_parser("results", help="print this run's per-row results as CSV")
    res.add_argument("--all", action="store_true", help="include the adversarial cases")
    args = ap.parse_args(argv)

    cmd = args.cmd or "check"
    if cmd == "import":
        print(bench.import_db(args.db, args.drop_missing))
        return 0
    if cmd == "refresh":
        print(bench.refresh())
        return 0
    if cmd == "calibrate":
        print(bench.calibrate())
        return 0
    if cmd == "drift":
        print(bench.drift(args.out))
        return 0
    run, _ = bench.run_bench()
    if cmd == "accept":
        ok, message = bench.accept(run, args.why, args.allow_regression)
        print(message)
        return 0 if ok else 1
    if cmd == "results":
        import csv

        writer = csv.DictWriter(sys.stdout, fieldnames=bench.RESULT_FIELDS[:-1], lineterminator="\n",
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(run.rows() if args.all else [bench.result_row(o) for o in run.corpus])
        return 0
    g = bench.gate(run, bench.load_baseline())
    print(bench.scoreboard(run, g, verbose=getattr(args, "verbose", False)))
    return 0 if g.ok else 1


if __name__ == "__main__":
    sys.exit(main())
