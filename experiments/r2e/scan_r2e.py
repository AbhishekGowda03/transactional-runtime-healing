"""Find R2E-Gym-Lite tasks that suit the healing evaluation.

For every task of the chosen repositories it runs the tests once WITHOUT healing (the `off`
setting of run_r2e.py) and records what the failure looks like:

  failing     the tests that should pass but do not (empty: the task is not usable)
  sites       repository frames below the test (where a repair could be placed)
  code        the source line of each of those frames, as the traceback prints it
  raise_only  every site is an explicit `raise` (never a heal point: not usable)
  usable      failing tests exist, sites exist, and at least one site is not a bare `raise`

It is resumable (tasks already in the output file are skipped) and removes each task's Docker
image afterwards, because every task has its own image (about 1-2 GB).

  uv run --with datasets python experiments/r2e/scan_r2e.py --repos scrapy,aiohttp,datalad \
      --per-repo 25 --out results/r2e/scan.jsonl

Afterwards:  python experiments/r2e/scan_r2e.py --summary results/r2e/scan.jsonl
"""
import argparse
import json
import os
import random
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_r2e as R  # noqa: E402


def sites_with_code(text, repo_root="/testbed"):
    """[(file:line, source line)] of the frames below the test, in traceback order."""
    found, seen = [], set()
    for m in re.finditer(R._HEADER, text, re.M | re.S):
        seen_test = False
        for f in re.finditer(r'File "([^"]+)", line (\d+), in [^\n]*\n[ \t]+([^\n]*)', m.group(2)):
            path = f.group(1)
            if not path.startswith(repo_root + "/") or "/.venv/" in path:
                continue
            rel = path[len(repo_root) + 1:]
            if R.is_test_path(rel) or rel.startswith("r2e_tests/"):
                seen_test = True
                continue
            site = f"{rel}:{f.group(2)}"
            if seen_test and site not in seen:
                seen.add(site)
                found.append((site, f.group(3).strip()))
    return found


def scan_one(row, a):
    commit = row["commit_hash"]
    rec = dict(commit=commit[:12], repo=row["repo_name"], time=time.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        text, code, _, _, secs = R.run_container(row, "off", a, None)
    except Exception as e:                                   # keep scanning
        rec.update(usable=False, status="CRASH", detail=repr(e)[:200])
        return rec
    outcome, counts, bad = R.judge(row, text, code)
    pairs = sites_with_code(text)
    rec.update(status=outcome, counts=counts, seconds=secs,
               failing={k: v[1] for k, v in list(bad.items())[:8]},
               sites=[s for s, _ in pairs], code=[c for _, c in pairs][:8],
               threads=bool(re.search(r"threading\.py|concurrent/futures|_thread", text)))
    non_raise = [c for _, c in pairs if not re.match(r"raise\b", c)]
    rec["raise_only"] = bool(pairs) and not non_raise
    rec["usable"] = bool(bad) and bool(pairs) and bool(non_raise)
    if outcome == "ERROR":
        rec["detail"] = text.strip()[-200:].replace("\n", " | ")
    return rec


def summary(path):
    rows = [json.loads(x) for x in open(path) if x.strip()]
    by = {}
    for r in rows:
        d = by.setdefault(r["repo"], dict(scanned=0, usable=0, no_failure=0, no_site=0,
                                          raise_only=0, error=0, threads=0))
        d["scanned"] += 1
        if r.get("usable"):
            d["usable"] += 1
            d["threads"] += bool(r.get("threads"))
        elif r.get("status") in ("ERROR", "CRASH"):
            d["error"] += 1
        elif not r.get("failing"):
            d["no_failure"] += 1
        elif r.get("raise_only"):
            d["raise_only"] += 1
        else:
            d["no_site"] += 1
    print(f"{'repo':10s} " + " ".join(f"{k:>10s}" for k in next(iter(by.values()), {})))
    for repo, d in sorted(by.items()):
        print(f"{repo:10s} " + " ".join(f"{v:>10d}" for v in d.values()))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", default="scrapy,aiohttp,datalad")
    ap.add_argument("--per-repo", type=int, default=25, help="tasks to scan per repository")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=os.path.join(R.ROOT, "results", "r2e", "scan.jsonl"))
    ap.add_argument("--workdir", default=os.path.expanduser("~/r2e_work"))
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--keep-images", action="store_true")
    ap.add_argument("--summary", metavar="FILE")
    a = ap.parse_args(argv)
    if a.summary:
        summary(a.summary)
        return 0
    a.workdir = os.path.abspath(os.path.expanduser(a.workdir))
    a.cache_dir = os.path.join(R.ROOT, "results", "llm_cache_r2e")
    a.max_calls, a.run_id, a.pool_threads, a.no_guard = 40, "scan", False, False
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    done = set()
    if os.path.exists(a.out):
        done = {json.loads(x)["commit"] for x in open(a.out) if x.strip()}
    from datasets import load_dataset
    wanted = [r.strip() for r in a.repos.split(",") if r.strip()]
    rows = {w: [] for w in wanted}
    for r in load_dataset("R2E-Gym/R2E-Gym-Lite", split="train"):
        if r["repo_name"] in rows:
            rows[r["repo_name"]].append(r)
    for repo in wanted:
        rs = sorted(rows[repo], key=lambda r: r["commit_hash"])
        random.Random(a.seed).shuffle(rs)
        todo = [r for r in rs[:a.per_repo] if r["commit_hash"][:12] not in done]
        print(f"{repo}: {len(rs)} tasks, scanning {len(todo)} of the first {a.per_repo}", flush=True)
        for r in todo:
            rec = scan_one(r, a)
            with open(a.out, "a") as f:
                f.write(json.dumps(rec) + "\n")
            print(f"  {rec['commit']} {rec['status']:7s} usable={rec.get('usable')} "
                  f"sites={rec.get('sites', [])[:2]}", flush=True)
            if not a.keep_images:
                subprocess.run(["docker", "rmi", "-f", r["docker_image"]], capture_output=True)
    summary(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())