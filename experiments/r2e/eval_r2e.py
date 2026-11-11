"""R2E-Gym-Lite evaluation: four settings x several runs over the usable tasks of scan_r2e.py.

settings   plain          HealBench-style: repair in place, no fork, no undo
           plain+guard    plain + HealGuard-inspired value matching (not HealGuard itself)
           fork-only      forked trial, validation point, effect blocking; no guard, no file undo
           full           the whole protocol (guard, file undo, post-commit blocking)

Per task it first runs the gold patch (the task must pass with it), then every setting for
every run. The model sees the same prompt in all settings of one run, so the settings are
compared on the same repairs. Runs >= 2 use temperature 0.7 (run 1 uses the default).
Each task's Docker image is removed when the task is done. Resumable.

  uv run --with datasets python experiments/r2e/eval_r2e.py --n 36 --runs 3
  python experiments/r2e/eval_r2e.py --summary results/r2e/eval.jsonl
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_r2e as R  # noqa: E402

SETTINGS = ["plain", "plain+guard", "fork-only", "full"]


def pick_tasks(scan_path, n, with_threads):
    rows = [json.loads(x) for x in open(scan_path) if x.strip()]
    rows = [r for r in rows if r.get("usable") and (with_threads or not r.get("threads"))]
    by = {}
    for r in rows:
        by.setdefault(r["repo"], []).append(r)
    order, i = [], 0
    while len(order) < n and any(by.values()):          # round robin over the repositories
        for repo in sorted(by):
            if by[repo] and len(order) < n:
                order.append(by[repo].pop(0))
        i += 1
    return order


def skipped_reasons(out):
    """Why the healer did not try anywhere (threads, handled by the caller, ...)."""
    res, hp = {}, os.path.join(out, "heal.jsonl")
    if os.path.exists(hp):
        for line in open(hp):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("status") == "SKIPPED":
                why = d.get("reason", "")
                key = "threads" if "thread" in why else ("handled" if "caller" in why else why[:40])
                res[key] = res.get(key, 0) + 1
    return res


def heal_detail(out):
    """Statuses of the attempts with their category (committed / safety / operational)."""
    cats, hp = {}, os.path.join(out, "heal.jsonl")
    if os.path.exists(hp):
        for line in open(hp):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("status") in ("SKIPPED", "NO_PROPOSAL"):
                continue
            c = d.get("category") or d.get("status")
            cats[c] = cats.get(c, 0) + 1
    return cats


def one(row, setting, run, sites, a):
    a.run_id = str(run)
    a.temperature = 0.7 if run >= 2 else None
    text, code, _, out, secs = R.run_container(row, setting, a, sites)
    outcome, counts, bad = R.judge(row, text, code)
    rec = dict(commit=row["commit_hash"][:12], repo=row["repo_name"], setting=setting, run=run,
               time=time.strftime("%Y-%m-%d %H:%M:%S"), outcome=outcome, counts=counts,
               seconds=secs, not_passing={k: v[1] for k, v in list(bad.items())[:5]})
    if setting in R.HEAL_SETTINGS:
        rec.update(R.summarize_healing(out))
        rec["categories"] = heal_detail(out)
        rec["skipped"] = skipped_reasons(out)
        rec["at_sites"] = R.records_at_sites(out, sites)
        if rec.get("model", {}).get("failed"):
            rec["outcome"] = "MODEL_ERROR"
        elif outcome != "PASSED" and not rec["at_sites"]:
            rec["outcome"] = "NOT_HEALABLE"
    if outcome == "ERROR":
        rec["detail"] = text.strip()[-200:].replace("\n", " | ")
    return rec


def summary(path):
    rows = [json.loads(x) for x in open(path) if x.strip()]
    gold = {r["commit"]: r["outcome"] for r in rows if r["setting"] == "gold"}
    ok = {c for c, o in gold.items() if o == "PASSED"}
    print(f"tasks with a passing gold run: {len(ok)} of {len(gold)}")
    rows = [r for r in rows if r["setting"] != "gold" and r["commit"] in ok]
    plain_pass = {(r["commit"], r["run"]) for r in rows if r["setting"] == "plain" and r["outcome"] == "PASSED"}
    hdr = ("setting", "runs", "PASSED", "FAILED", "NOT_HEAL", "other", "sec/run",
           "committed", "safety-rej", "oper-rej", "no-live(thr)", "false-rej")
    print("%-12s" % hdr[0] + "".join("%12s" % h for h in hdr[1:]))
    for s in SETTINGS:
        rs = [r for r in rows if r["setting"] == s]
        if not rs:
            continue
        n = len(rs)
        c = lambda o: sum(r["outcome"] == o for r in rs)  # noqa: E731
        cat = lambda k: sum(r.get("categories", {}).get(k, 0) for r in rs)  # noqa: E731
        thr = sum(1 for r in rs if r.get("skipped", {}).get("threads"))
        # false rejection: the same run passed in plain mode but this setting did not,
        # and this setting rejected something by a safety rule
        fr = sum(1 for r in rs if (r["commit"], r["run"]) in plain_pass and r["outcome"] != "PASSED"
                 and r.get("categories", {}).get("safety"))
        other = n - c("PASSED") - c("FAILED") - c("NOT_HEALABLE")
        sec = sum(r["seconds"] for r in rs) / n
        print("%-12s" % s + "".join("%12s" % v for v in (n, c("PASSED"), c("FAILED"), c("NOT_HEALABLE"),
                                                         other, round(sec, 1), cat("committed"),
                                                         cat("safety"), cat("operational"), thr, fr)))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", default=os.path.join(R.ROOT, "results", "r2e", "scan.jsonl"))
    ap.add_argument("--out", default=os.path.join(R.ROOT, "results", "r2e", "eval.jsonl"))
    ap.add_argument("--n", type=int, default=36, help="number of tasks (round robin over repos)")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--settings", default=",".join(SETTINGS))
    ap.add_argument("--with-threads", action="store_true",
                    help="also use tasks whose failing run showed thread use")
    ap.add_argument("--pool-threads", action="store_true")
    ap.add_argument("--workdir", default=os.path.expanduser("~/r2e_work"))
    ap.add_argument("--max-calls", type=int, default=60)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--only", help="only commits starting with this prefix")
    ap.add_argument("--keep-images", action="store_true")
    ap.add_argument("--summary", metavar="FILE")
    a = ap.parse_args(argv)
    if a.summary:
        summary(a.summary)
        return 0
    a.workdir = os.path.abspath(os.path.expanduser(a.workdir))
    a.cache_dir = os.path.join(R.ROOT, "results", "llm_cache_r2e")
    a.no_guard = False
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    done = set()
    if os.path.exists(a.out):
        for x in open(a.out):
            if x.strip():
                d = json.loads(x)
                done.add((d["commit"], d["setting"], d["run"]))
    tasks = pick_tasks(a.scan, a.n, a.with_threads)
    if a.only:
        tasks = [t for t in tasks if t["commit"].startswith(a.only)]
    settings = [s for s in a.settings.split(",") if s]
    print(f"{len(tasks)} tasks, settings {settings}, {a.runs} runs", flush=True)
    for t in tasks:
        row = R.load_row(t["commit"])
        sites = t["sites"]
        jobs = ([("gold", 1)] if (t["commit"], "gold", 1) not in done else []) + \
               [(s, r) for r in range(1, a.runs + 1) for s in settings if (t["commit"], s, r) not in done]
        for setting, run in jobs:
            rec = one(row, setting, run, sites, a)
            with open(a.out, "a") as f:
                f.write(json.dumps(rec) + "\n")
            print(f"{t['commit']} {t['repo']:8s} {setting:12s} r{run} {rec['outcome']:12s} "
                  f"{rec['seconds']:6.1f}s heals={rec.get('categories')} skipped={rec.get('skipped')}",
                  flush=True)
        if not a.keep_images:
            subprocess.run(["docker", "rmi", "-f", row["docker_image"]], capture_output=True)
    summary(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())