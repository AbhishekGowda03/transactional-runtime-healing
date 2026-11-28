"""Re-screen a scan with the current rule for test support (no image is pulled, nothing is run).

A scan marks a task usable if a failing run has a traceback frame below the test in the repository's
code that is not a bare `raise`. Frames in test helpers (pandas/_testing, Tests/helper.py,
scrapy/utils/test.py, aiohttp/pytest_plugin.py, ...) never get heal points, so a task whose only such
frames are helpers is not healable. This recomputes `usable` from the stored sites with the loader's
rule (repo_hook.excluded_parts) and only ever downgrades a record. The old file is kept as .bak.

    uv run python experiments/r2e/rescreen_scan.py results/r2e/scan_py37.jsonl
"""
import argparse
import collections
import json
import os
import re
import shutil
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
from transactional_runtime_healing import repo_hook  # noqa: E402


def keep_site(site):
    rel = site.rpartition(":")[0]
    return not repo_hook.excluded_parts([x for x in rel.split("/") if x])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("scan")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    rows = [json.loads(x) for x in open(a.scan) if x.strip()]
    down = collections.Counter()
    total = collections.Counter()
    for r in rows:
        if not r.get("usable") or not r.get("sites"):
            continue
        codes = r.get("code") or []
        pairs = [(s, codes[i] if i < len(codes) else None) for i, s in enumerate(r["sites"])]
        kept = [(s, c) for s, c in pairs if keep_site(s)]
        non_raise = [c for _, c in kept if c is None or not re.match(r"raise\b", c)]
        total[r["repo"]] += 1
        if kept and non_raise:
            if len(kept) != len(pairs):
                r["sites_all"], r["sites"] = r["sites"], [s for s, _ in kept]
                r["code"] = [c for _, c in kept if c is not None][:8]
            continue
        r["usable_before"], r["usable"] = True, False
        r["rescreened"] = "only test support below the test" if not kept else "only bare raise left"
        down[r["repo"]] += 1
        print(f"  {r['repo']:8s} {r['commit']}  not usable now: {r['rescreened']}  "
              f"{[s for s, _ in pairs][:3]}")
    for repo in sorted(total):
        print(f"{repo}: {total[repo] - down[repo]} of {total[repo]} usable tasks stay, {down[repo]} dropped")
    if not a.dry_run and sum(down.values()) + 0 >= 0:
        shutil.copy(a.scan, a.scan + ".prerescreen")
        with open(a.scan, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")


if __name__ == "__main__":
    main()