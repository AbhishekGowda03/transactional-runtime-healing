"""Compare the per-case verdicts of two StateLeak runs (the .json written by --out, or the printed output of run_stateleak.py).

    uv run python experiments/compat/compare_stateleak.py A.json B.json [--label-a 3.12 --label-b 3.7]

Prints every (setting, case) whose status or verdict differs, and the number of cases compared.
Used to check that the Python 3.7 tier (wrapper hooks, pre-instrumented programs) gives the same safety and
rollback results as the other tiers, and to name the cases where it does not.
"""
import argparse
import re
import sys

LINE = re.compile(r"^\s{3}(\S+)\s+(good|bad|reject-only)\s+(\S+)\s+(.+?)\s*$")


def parse(path):
    """{(setting, case): (label, status, verdict)} from the runner's .json file or its printed output."""
    if path.endswith(".json"):
        import json
        return {(r["setting"], r["case"]): (r["label"], r["status"], r["verdict"])
                for r in json.load(open(path))}
    out, setting = {}, None
    for line in open(path):
        m = re.match(r"^\[run \] (.+)$", line.rstrip())
        if m:
            setting = m.group(1)
            continue
        m = LINE.match(line.rstrip("\n"))
        if m and setting:
            out[(setting, m.group(1))] = (m.group(2), m.group(3), m.group(4))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--label-a", default="A")
    ap.add_argument("--label-b", default="B")
    ns = ap.parse_args(argv)
    A, B = parse(ns.a), parse(ns.b)
    both = sorted(set(A) & set(B))
    diff = [k for k in both if A[k] != B[k]]
    only = sorted(set(A) ^ set(B))
    print(f"{len(both)} (setting, case) pairs in both files, {len(diff)} differ, {len(only)} only in one")
    for k in diff:
        print(f"  {k[0]:20s} {k[1]:34s} {ns.label_a}: {A[k][1]} / {A[k][2]}   {ns.label_b}: {B[k][1]} / {B[k][2]}")
    for k in only:
        print(f"  only in {ns.label_a if k in A else ns.label_b}: {k}")
    return 1 if diff or only else 0


if __name__ == "__main__":
    sys.exit(main())