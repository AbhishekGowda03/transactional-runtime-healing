"""Which files did the committed repairs of the passing runs touch?

For every PASSED run of an evaluation file this reads the run's heal log (~/r2e_work/<commit>_<setting>
[_rN]/out/heal.jsonl) and lists the committed repairs that sit in test support (pandas/_testing, test
helpers, ...) or repair an assertion. A pass whose committed repairs are all in library code is a real
pass; a pass with a repair in a helper may be fake and needs a rerun with the current exclusion rule.

    uv run python experiments/r2e/audit_repairs.py results/r2e/eval_py37_pandas.jsonl
"""
import argparse
import collections
import json
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
from transactional_runtime_healing import repo_hook  # noqa: E402

ASSERT = re.compile(r"(^|\n)\s*assert\b|\bassert_\w*\s*\(|\bAssertionError\b|\bassert[A-Z]\w*\s*\(")


def rel(path):
    return path[len("/testbed/"):] if path.startswith("/testbed/") else path


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("eval_file")
    ap.add_argument("--workdir", default=os.path.expanduser("~/r2e_work"))
    ap.add_argument("--list", action="store_true", help="print every suspect committed repair")
    a = ap.parse_args()
    rows = [json.loads(x) for x in open(a.eval_file) if x.strip()]
    tally = collections.defaultdict(lambda: collections.Counter())
    per_task = collections.defaultdict(lambda: collections.Counter())
    for r in rows:
        if r["setting"] not in ("plain", "plain+guard", "fork-only", "full") or r["outcome"] != "PASSED":
            continue
        tag = "" if str(r["run"]) in ("1", "") else f"_r{r['run']}"
        hp = os.path.join(a.workdir, f"{r['commit']}_{r['setting']}{tag}", "out", "heal.jsonl")
        if not os.path.exists(hp):
            tally[r["setting"]]["no log"] += 1
            continue
        bad = []
        for line in open(hp):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("status") != "COMMITTED":
                continue
            rp = rel(d["file"])
            why = []
            if repo_hook.excluded_parts([x for x in rp.split("/") if x]):
                why.append("in test support")
            if d.get("assertion_repair") or ASSERT.search(d.get("statement") or "") or \
                    "AssertionError" in (d.get("exception") or ""):
                why.append("repairs an assertion")
            if why:
                bad.append((rp, d["line"], " + ".join(why), (d.get("statement") or "")[:70]))
        key = "suspect" if bad else "library only"
        tally[r["setting"]][key] += 1
        per_task[r["commit"]][key] += 1
        if bad and a.list:
            for b in bad[:6]:
                print(f"  {r['commit']} {r['setting']:6s} r{r['run']}  {b[0]}:{b[1]}  [{b[2]}]  {b[3]!r}")
    print("\nPASSED runs by what their committed repairs touched:")
    for s, c in sorted(tally.items()):
        print(f"  {s:12s} library only {c['library only']:3d}   suspect {c['suspect']:3d}   no log {c['no log']:3d}")
    print("\nper task (passed runs of all settings):")
    for t, c in sorted(per_task.items()):
        print(f"  {t}  library only {c['library only']:2d}  suspect {c['suspect']:2d}")


if __name__ == "__main__":
    main()