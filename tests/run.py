"""Run the tests in parallel: every test on its own, spread over worker processes, slowest first.

    python tests/run.py                  all tests, one worker per CPU
    python tests/run.py routing opencode only tests whose id contains one of these words (case-insensitive)
    python tests/run.py --failed         only the tests that failed in the last run
    python tests/run.py -j 4 --slow 10   4 workers; list the 10 slowest tests at the end

Each worker process runs one test at a time with its output buffered (as `unittest -b`), so a failure shows its
own output only. The durations of the last run are kept in tests/.durations.json and decide the order next time:
the slowest tests start first, so the run ends when the longest test ends instead of when the last one starts.
Failed test ids are kept in tests/.last_failed. Exit status: 0 if everything passed, 1 otherwise.
"""
from __future__ import annotations

import argparse
import io
import json
import multiprocessing as mp
import os
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DURATIONS = HERE / ".durations.json"
LAST_FAILED = HERE / ".last_failed"


def _setup() -> None:
    for p in (str(HERE), str(HERE.parent)):  # tests import each other (`from test_orch import ...`) and orch
        if p not in sys.path:
            sys.path.insert(0, p)


def _ids(suite: unittest.TestSuite) -> list:
    out = []
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            out += _ids(t)
        else:
            out.append(t.id())
    return out


def discover() -> list:
    _setup()
    return _ids(unittest.defaultTestLoader.discover(str(HERE), pattern="test_*.py", top_level_dir=str(HERE)))


def run_one(test_id: str) -> tuple:
    """(id, outcome, seconds, report) for one test, run in this worker process."""
    _setup()
    t0 = time.monotonic()
    stream = io.StringIO()
    try:
        suite = unittest.defaultTestLoader.loadTestsFromName(test_id)
    except Exception as e:  # noqa: BLE001  an import error shows as the test's error
        return test_id, "error", 0.0, f"could not load: {e!r}"
    result = unittest.TextTestResult(stream, descriptions=False, verbosity=0)
    result.buffer = True  # a test's prints show only when it fails
    suite.run(result)
    secs = time.monotonic() - t0
    if result.errors or result.failures:
        report = "\n".join(text for _t, text in result.errors + result.failures)
        return test_id, "error" if result.errors else "fail", secs, report
    if result.skipped:
        return test_id, "skip", secs, ""
    return test_id, "ok", secs, ""


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run orch's tests in parallel.")
    ap.add_argument("words", nargs="*", help="run only tests whose id contains one of these (case-insensitive)")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4, help="worker processes (default: CPUs)")
    ap.add_argument("--failed", action="store_true", help="only the tests that failed last time")
    ap.add_argument("--slow", type=int, default=0, metavar="N", help="list the N slowest tests at the end")
    a = ap.parse_args(argv)

    ids = discover()
    if a.failed:
        last = set(LAST_FAILED.read_text().split()) if LAST_FAILED.exists() else set()
        ids = [i for i in ids if i in last]
    if a.words:
        words = [w.lower() for w in a.words]
        ids = [i for i in ids if any(w in i.lower() for w in words)]
    if not ids:
        print("no tests selected")
        return 0
    known = _read_json(DURATIONS)
    ids.sort(key=lambda i: -float(known.get(i, 1.0)))  # slowest first; a new test counts as 1 s

    jobs = max(1, min(a.jobs, len(ids)))
    t0 = time.monotonic()
    results = []
    marks = {"ok": ".", "skip": "s", "fail": "F", "error": "E"}
    with mp.get_context("spawn").Pool(jobs, initializer=_setup) as pool:
        for r in pool.imap_unordered(run_one, ids):
            results.append(r)
            sys.stdout.write(marks[r[1]])
            sys.stdout.flush()
    wall = time.monotonic() - t0
    print()

    bad = sorted((r for r in results if r[1] in ("fail", "error")), key=lambda r: r[0])
    for test_id, outcome, _s, report in bad:
        print("=" * 70)
        print(f"{outcome.upper()}: {test_id}")
        print("-" * 70)
        print(report.rstrip())
    if a.slow:
        print(f"\nslowest {a.slow}:")
        for test_id, _o, secs, _r in sorted(results, key=lambda r: -r[2])[:a.slow]:
            print(f"  {secs:6.2f}s  {test_id}")

    known.update({r[0]: round(r[2], 3) for r in results})
    try:
        DURATIONS.write_text(json.dumps(known, indent=0, sort_keys=True))
        LAST_FAILED.write_text("\n".join(r[0] for r in bad) + ("\n" if bad else ""))
    except OSError:
        pass
    n = {k: sum(1 for r in results if r[1] == k) for k in marks}
    total = sum(r[2] for r in results)
    print(f"\nRan {len(results)} tests in {wall:.1f}s on {jobs} workers ({total:.1f}s of test time): "
          f"{n['ok']} ok, {n['skip']} skipped, {n['fail']} failed, {n['error']} errors")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
