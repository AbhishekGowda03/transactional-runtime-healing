"""Look at SWE-bench Lite and list candidate instances for the healing experiments.

    uv run --with datasets python experiments/swebench/inspect_lite.py [--repo sympy/sympy] [--max-tests 3]

What it prints: for every repository, how many instances there are, which versions, and how
many have few failing tests and a small gold patch (the easy ones). It writes the candidates to
results/swebench/candidates.json. Nothing is run; this only reads the dataset
(princeton-nlp/SWE-bench_Lite, downloaded once by the `datasets` package).

The healing experiments need instances whose failing test ends in a RUNTIME EXCEPTION, not in a
failed assertion. The dataset does not say which; that is decided later by running the buggy
code (classify step). Here we only narrow the list.
"""
import argparse
import collections
import json
import os


def _tests(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return []
    return list(value or [])


def _patch_files(patch):
    return sum(1 for line in (patch or "").splitlines() if line.startswith("diff --git"))


def summarize(rows, repo=None, max_tests=3, max_patch_files=1):
    """(per-repo table, candidate list) from dataset rows (dicts)."""
    per = collections.defaultdict(lambda: dict(n=0, versions=set(), easy=0))
    cands = []
    for r in rows:
        if repo and r["repo"] != repo:
            continue
        p = per[r["repo"]]
        p["n"] += 1
        p["versions"].add(r.get("version", "?"))
        f2p = _tests(r.get("FAIL_TO_PASS"))
        files = _patch_files(r.get("patch"))
        if 0 < len(f2p) <= max_tests and files <= max_patch_files:
            p["easy"] += 1
            cands.append(dict(instance_id=r["instance_id"], repo=r["repo"],
                              version=r.get("version"), base_commit=r["base_commit"],
                              fail_to_pass=f2p, patch_files=files))
    return per, cands


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="princeton-nlp/SWE-bench_Lite")
    ap.add_argument("--repo", help="only this repository, e.g. sympy/sympy")
    ap.add_argument("--max-tests", type=int, default=3, help="most failing tests per instance")
    ap.add_argument("--out", default=os.path.join("results", "swebench", "candidates.json"))
    ns = ap.parse_args()
    from datasets import load_dataset
    rows = list(load_dataset(ns.dataset, split="test"))
    per, cands = summarize(rows, ns.repo, ns.max_tests)
    print(f"{len(rows)} instances in {ns.dataset}\n")
    print(f"{'repository':32} {'instances':>9} {'easy':>5}  versions")
    for repo, p in sorted(per.items(), key=lambda kv: -kv[1]["n"]):
        vs = sorted(p["versions"], key=str)
        print(f"{repo:32} {p['n']:>9} {p['easy']:>5}  {', '.join(map(str, vs[:8]))}"
              f"{' ...' if len(vs) > 8 else ''}")
    os.makedirs(os.path.dirname(ns.out), exist_ok=True)
    with open(ns.out, "w") as f:
        json.dump(cands, f, indent=1)
    print(f"\n{len(cands)} candidates (<= {ns.max_tests} failing tests, one-file gold patch) "
          f"written to {ns.out}")


if __name__ == "__main__":
    main()