"""Which HealBench tasks run on a Python the instrumenter supports (3.9 or newer)?

HealBench (github.com/IntelligentDDS/HealGuard, datasets/healbench.jsonl) has 265 tasks:
217 from R2E-Gym-Lite (image namanjain12/<repo>_final:<commit>, interpreter /testbed/.venv/bin/python)
and 48 from SWE-bench variants (other image layouts, not probed here: reported as "not probed").

For each R2E task the script starts the task's image once and prints the interpreter version.
The first start pulls the image; --rmi removes it again afterwards to save disk.
Results go to a jsonl file; the script is resumable (finished tasks are skipped, failed probes are\nretried,\nand it stops after 5 failures in a row so that a broken Docker does not burn hours).

  python experiments/healbench/python_versions.py --dataset ~/HealGuard/datasets/healbench.jsonl \
      --out results/healbench/python_versions.jsonl --per-repo 5 --rmi      # a quick sample first
  python experiments/healbench/python_versions.py --dataset ... --out ... --rmi   # all 217
  python experiments/healbench/python_versions.py --dataset ... --out ... --summary
"""
import argparse
import collections
import json
import os
import re
import subprocess
import sys
import time


def probe(image, timeout=1800):
    """([major, minor], "") or (None, reason). The reason keeps docker's own message."""
    try:
        p = subprocess.run(["docker", "run", "--rm", image, "/testbed/.venv/bin/python", "-c",
                            "import sys; print(sys.version_info[0], sys.version_info[1])"],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception as e:
        return None, repr(e)[:300]
    for line in reversed(p.stdout.splitlines()):          # docker may print notices around the answer
        m = re.fullmatch(r"\s*(\d+) (\d+)\s*", line)
        if m:
            return [int(m.group(1)), int(m.group(2))], ""
    return None, f"exit {p.returncode}: stdout={p.stdout.strip()[-150:]!r} stderr={p.stderr.strip()[-150:]!r}"


def load(path):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def summary(rows, done):
    by = collections.defaultdict(collections.Counter)
    for r in rows:
        repo = r["repo"].split("/")[-1]
        if r["source_dataset"] != "R2E-Gym-Lite":
            by[repo]["not probed"] += 1
            continue
        rec = done.get(r["instance_id"])
        if rec is None:
            by[repo]["not yet probed"] += 1
        elif rec["python"] is None:
            by[repo]["probe failed"] += 1
        elif tuple(rec["python"]) >= (3, 9):
            by[repo]["3.9+"] += 1
        else:
            by[repo]["below 3.9"] += 1
    keys = ["3.9+", "below 3.9", "probe failed", "not yet probed", "not probed"]
    print(f"{'repo':16}" + "".join(f"{k:>16}" for k in keys) + f"{'total':>8}")
    tot = collections.Counter()
    for repo, c in sorted(by.items(), key=lambda kv: -sum(kv[1].values())):
        print(f"{repo:16}" + "".join(f"{c[k]:>16}" for k in keys) + f"{sum(c.values()):>8}")
        tot.update(c)
    print(f"{'all':16}" + "".join(f"{tot[k]:>16}" for k in keys) + f"{sum(tot.values()):>8}")
    versions = collections.Counter(tuple(r["python"]) for r in done.values() if r["python"])
    print("interpreter versions:", {f"{a}.{b}": n for (a, b), n in sorted(versions.items())})


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--per-repo", type=int, default=0, help="probe at most this many R2E tasks per repo")
    ap.add_argument("--rmi", action="store_true", help="remove each image after probing it")
    ap.add_argument("--summary", action="store_true")
    a = ap.parse_args(argv)
    rows = load(a.dataset)
    done = {}
    if os.path.exists(a.out):
        for r in load(a.out):                       # failed probes are retried, later lines win
            if r["python"] is not None:
                done[r["instance_id"]] = r
    if not a.summary:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        taken = collections.Counter()
        failures = 0
        for r in rows:
            if r["source_dataset"] != "R2E-Gym-Lite" or not r["dockerhub_tag"]:
                continue
            repo = r["repo"].split("/")[-1]
            if a.per_repo and taken[repo] >= a.per_repo:
                continue
            taken[repo] += 1
            if r["instance_id"] in done:
                continue
            image = r["dockerhub_tag"]
            py, err = probe(image)
            rec = dict(instance_id=r["instance_id"], repo=repo, image=image, python=py, error=err,
                       time=time.strftime("%Y-%m-%d %H:%M:%S"))
            done[r["instance_id"]] = rec
            with open(a.out, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            print(repo, image.split(":")[-1][:12], py, err[:200], flush=True)
            failures = failures + 1 if py is None else 0
            if failures >= 5:
                print("5 probes in a row failed: stopping (see the message above); rerun to retry")
                break
            if a.rmi:
                subprocess.run(["docker", "rmi", "-f", image], capture_output=True)
    summary(rows, done)


if __name__ == "__main__":
    main()