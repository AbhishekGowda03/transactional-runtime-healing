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


def row_tier(r):
    """py37, py38 or py39+ of a scan record (older scans have only the interpreter's version)."""
    if r.get("tier"):
        return r["tier"]
    py = r.get("python")
    if not py:
        return "py39+"
    return "py37" if tuple(py) < (3, 8) else "py38" if tuple(py) < (3, 9) else "py39+"


def pick_tasks(scan_path, n, with_threads, repos=None, tier="py39+"):
    rows = [json.loads(x) for x in open(scan_path) if x.strip()]
    for r in rows:
        r["tier"] = row_tier(r)
    # tasks on Python before 3.9 were marked old_python (not usable) by the scan; they are usable
    # since their repository can be pre-instrumented (pre_instrument.py)
    rows = [r for r in rows if (r.get("usable") or (r.get("old_python") and r["tier"] != "py39+"))
            and (with_threads or not r.get("threads"))
            and (not repos or r["repo"] in repos)
            and (tier == "all" or r["tier"] == tier)]
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
    heal_detail.names = {}
    heal_detail.assertion = 0          # committed repairs of an assertion (they can fake a pass)
    heal_detail.secs = []
    heal_detail.rejected = []
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
            if d.get("status") == "COMMITTED" and d.get("assertion_repair"):
                heal_detail.assertion += 1
            heal_detail.names[d["status"]] = heal_detail.names.get(d["status"], 0) + 1
            if d.get("attempt_seconds") is not None:
                heal_detail.secs.append(d["attempt_seconds"])
            if d["status"] in ("REJECTED_CHECK", "REJECTED_EXTERNAL_EFFECT", "REJECTED_PROTECTED_OPERATION",
                               "REJECTED_RECURRENCE"):
                heal_detail.rejected.append(dict(status=d["status"], site=f"{os.path.basename(d['file'])}:{d['line']}",
                                                 reason=d.get("reason", "")[:100], effects=d.get("effects", [])[:3],
                                                 code=(d.get("code") or "")[:100]))
    return cats


def one(row, setting, run, sites, a, tier="py39+"):
    a.run_id = str(run)
    a.temperature = 0.7 if run >= 2 else None
    text, code, _, out, secs = R.run_container(row, setting, a, sites)
    outcome, counts, bad = R.judge(row, text, code)
    if setting in R.HEAL_SETTINGS and ("has no attribute 'get_source_segment'" in text
                                       or "has no attribute 'unparse'" in text):
        outcome = "UNSUPPORTED_PY"        # the repository's Python is older than 3.9 (ast.unparse)
    rec = dict(commit=row["commit_hash"][:12], repo=row["repo_name"], tier=tier, setting=setting, run=run,
               time=time.strftime("%Y-%m-%d %H:%M:%S"), outcome=outcome, counts=counts,
               seconds=secs, not_passing={k: v[1] for k, v in list(bad.items())[:5]})
    if setting in R.HEAL_SETTINGS:
        rec.update(R.summarize_healing(out))
        rec["categories"] = heal_detail(out)
        rec["statuses"] = dict(heal_detail.names)     # COMMITTED, REJECTED_CHECK, ...
        rec["attempt_seconds"] = list(heal_detail.secs)   # trial to verdict, without the model's time
        rec["rejected"] = list(heal_detail.rejected)
        rec["assertion_repairs"] = heal_detail.assertion
        rec["skipped"] = skipped_reasons(out)
        rec["at_sites"] = R.records_at_sites(out, sites)
        # attempts skipped for a reason of the protocol itself (not a thread or a caller that handles
        # the error) and nothing tried at all: the setting did not run; never count it as a result
        odd = {k: v for k, v in rec["skipped"].items() if k not in ("threads", "handled")}
        if odd and not rec.get("heals") and outcome != "UNSUPPORTED_PY":
            rec["outcome"] = outcome = "INVALID_SKIP"
            rec["detail"] = "attempts skipped: " + "; ".join(sorted(odd))
        if outcome in ("UNSUPPORTED_PY", "INVALID_SKIP"):
            pass
        elif rec.get("model", {}).get("failed"):
            rec["outcome"] = "MODEL_ERROR"
        elif outcome == "FAILED" and not rec["at_sites"]:
            rec["outcome"] = "NOT_HEALABLE"
    if outcome == "ERROR":
        rec["detail"] = text.strip()[-200:].replace("\n", " | ")
    return rec


def per_task(rows, ok):
    """One line per task: passes out of the runs, per setting; then the runs where settings differ."""
    runs = {}
    for r in rows:
        if r["commit"] in ok and r["setting"] in SETTINGS:
            runs.setdefault(r["commit"], {}).setdefault(r["setting"], {})[r["run"]] = r
    print("\nper task (passes / runs)")
    print("%-14s %-8s" % ("commit", "repo") + "".join("%13s" % s for s in SETTINGS))
    for c, by in sorted(runs.items(), key=lambda kv: (next(iter(next(iter(kv[1].values())).values()))["repo"], kv[0])):
        repo = next(iter(next(iter(by.values())).values()))["repo"]
        cells = []
        for s in SETTINGS:
            rs = by.get(s, {})
            cells.append("%d/%d" % (sum(x["outcome"] == "PASSED" for x in rs.values()), len(rs)))
        print("%-14s %-8s" % (c, repo) + "".join("%13s" % x for x in cells))
    print("\nruns where a setting differs from plain")
    for c, by in sorted(runs.items()):
        for run, base in sorted(by.get("plain", {}).items()):
            for s in SETTINGS[1:]:
                x = by.get(s, {}).get(run)
                if x and x["outcome"] != base["outcome"]:
                    print(f"  {c} r{run} plain={base['outcome']} {s}={x['outcome']} "
                          f"statuses={x.get('statuses')} skipped={x.get('skipped')}")


TIER_NOTE = {"py37": "Python 3.7: effects are noticed by wrappers (compat_hooks.py), not audit hooks; "
                     "direct use of io.FileIO and _socket.socket.connect is NOT noticed. "
                     "No claim of complete protection; do not merge with the other tiers.",
             "py38": "Python 3.8: audit hooks are available; the instrumentation is done ahead of time."}


def summary(path, detail=False, tier=None):
    """The table for each Python tier, kept apart (3.7 detects fewer operations)."""
    all_rows = [json.loads(x) for x in open(path) if x.strip()]
    tiers = sorted({r.get("tier", "py39+") for r in all_rows})
    if tier:
        tiers = [tier]
    for tr in tiers:
        if len(tiers) > 1 or tier:
            print(f"\n=== tier {tr} ===")
            if tr in TIER_NOTE:
                print(TIER_NOTE[tr])
        _summary([r for r in all_rows if r.get("tier", "py39+") == tr], detail)


def _summary(rows, detail=False):
    bad = [r for r in rows if r["outcome"] == "INVALID_SKIP"]
    if bad:
        why = sorted({r.get("detail", "") for r in bad})
        print(f"WARNING: {len(bad)} runs are INVALID_SKIP: the healer skipped its attempts for a reason of "
              f"the protocol itself and tried nothing, so these runs say nothing about the protection. "
              f"They are left out of the rates below.\n  {why}")
    gold = {r["commit"]: r["outcome"] for r in rows if r["setting"] == "gold"}
    ok = {c for c, o in gold.items() if o == "PASSED"}
    print(f"tasks with a passing gold run: {len(ok)} of {len(gold)}")
    unsup = {r["commit"] for r in rows if r["outcome"] == "UNSUPPORTED_PY"}
    rows = [r for r in rows if r["setting"] != "gold" and r["commit"] in ok and r["commit"] not in unsup
            and r["outcome"] != "INVALID_SKIP"]
    if unsup:
        print(f"tasks the instrumenter cannot handle (repository Python < 3.9): {len(unsup)} {sorted(unsup)}")
    plain_pass = {(r["commit"], r["run"]) for r in rows if r["setting"] == "plain" and r["outcome"] == "PASSED"}
    hdr = ("setting", "runs", "PASSED", "FAILED", "NOT_HEAL", "other", "median s",
           "committed", "repair-err", "rule-rej", "oper-rej", "no-live(thr)", "false-rej")
    print("%-12s" % hdr[0] + "".join("%13s" % h for h in hdr[1:]))
    for s in (["off"] if any(r["setting"] == "off" for r in rows) else []) + SETTINGS:
        rs = [r for r in rows if r["setting"] == s]
        if not rs:
            continue
        n = len(rs)
        c = lambda o: sum(r["outcome"] == o for r in rs)  # noqa: E731
        cat = lambda k: sum(r.get("categories", {}).get(k, 0) for r in rs)  # noqa: E731
        RULES = ("REJECTED_CHECK", "REJECTED_EXTERNAL_EFFECT", "REJECTED_PROTECTED_OPERATION",
                 "REJECTED_RECURRENCE")
        nrule = lambda r: sum(r.get("statuses", {}).get(k, 0) for k in RULES)  # noqa: E731
        thr = sum(1 for r in rs if r.get("skipped", {}).get("threads"))
        # false rejection: the same run passed in plain mode but this setting did not,
        # and this setting rejected something by a safety rule
        fr = sum(1 for r in rs if (r["commit"], r["run"]) in plain_pass and r["outcome"] != "PASSED"
                 and nrule(r))
        other = n - c("PASSED") - c("FAILED") - c("NOT_HEALABLE")
        secs = sorted(r["seconds"] for r in rs if not (s == "off" and r["run"] == 1))  # run 1 of `off` pulls the image
        sec = secs[len(secs) // 2] if secs else 0
        print("%-12s" % s + "".join("%13s" % v for v in (n, c("PASSED"), c("FAILED"), c("NOT_HEALABLE"),
                                                         other, round(sec, 1), cat("committed"),
                                                         sum(r.get("statuses", {}).get("REJECTED_REPAIR_ERROR", 0) for r in rs),
                                                         sum(nrule(r) for r in rs), cat("operational"),
                                                         thr, fr)))
    sus = [r for r in rows if r["outcome"] == "PASSED" and r.get("assertion_repairs")]
    if sus:
        print(f"\nWARNING: {len(sus)} PASSED runs committed a repair of an assertion (a statement with assert / "
              f"assert_* / AssertionError). Such a pass can come from the test helper, not from the library: "
              f"check them ({sorted({r['commit'] for r in sus})[:8]}). Logs from before this flag existed "
              f"do not have it.")
    off = {(r["commit"], r["run"]): r["seconds"] for r in rows if r["setting"] == "off" and r["run"] >= 2}
    if off:
        print("\ntime relative to `off` (same task and run, runs 2-3, median of ratios; the first run of a task is left out"
              " because it includes the image pull)")
        for s in SETTINGS:
            ratios = sorted(r["seconds"] / off[(r["commit"], r["run"])] for r in rows
                            if r["setting"] == s and (r["commit"], r["run"]) in off and off[(r["commit"], r["run"])] > 0)
            if ratios:
                print(f"  {s:12s} median x{ratios[len(ratios) // 2]:.2f}   (mean x{sum(ratios) / len(ratios):.2f}, n={len(ratios)})")
    print("\ntime per heal attempt in seconds (trial to verdict; the model's response time is not included)")
    for s in SETTINGS:
        v = sorted(x for r in rows if r["setting"] == s for x in r.get("attempt_seconds", []))
        if v:
            print(f"  {s:12s} attempts {len(v):5d}  median {v[len(v) // 2]:.4f}  mean {sum(v) / len(v):.4f}  "
                  f"p90 {v[int(len(v) * 0.9)]:.4f}")
    mc = {}
    for r in rows:
        if r["setting"] in R.HEAL_SETTINGS and r.get("model"):
            m = mc.setdefault(r["setting"], [0, 0])
            m[0] += r["model"].get("from_model", 0)
            m[1] += r["model"].get("from_cache", 0)
    if mc:
        print("\nmodel answers per setting: real calls / from cache")
        for s in SETTINGS:
            if s in mc:
                print(f"  {s:12s} {mc[s][0]:5d} / {mc[s][1]:5d}")
    rej = [(r, x) for r in rows for x in r.get("rejected", [])]
    if rej:
        print(f"\nrejections by a safety rule ({len(rej)}): which repair, which effect")
        for r, x in rej:
            print(f"  {r['commit']} {r['setting']:11s} r{r['run']} {x['status'].replace('REJECTED_', ''):18s} "
                  f"{x['site']:28s} effects={x['effects']} | {x['code']!r}")
    if detail:
        per_task(rows, ok)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", default=os.path.join(R.ROOT, "results", "r2e", "scan.jsonl"))
    ap.add_argument("--out", default=os.path.join(R.ROOT, "results", "r2e", "eval.jsonl"))
    ap.add_argument("--n", type=int, default=36, help="number of tasks (round robin over repos)")
    ap.add_argument("--repos", help="only these repositories, e.g. pillow")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--settings", default=",".join(SETTINGS))
    ap.add_argument("--tier", choices=("py39+", "py38", "py37", "all"),
                    help="which Python tier to evaluate (default py39+; kept apart because 3.7 uses "
                         "wrapper-based detection); with --summary: only this tier's table")
    ap.add_argument("--with-threads", action="store_true",
                    help="also use tasks whose failing run showed thread use")
    ap.add_argument("--pool-threads", action="store_true")
    ap.add_argument("--workdir", default=os.path.expanduser("~/r2e_work"))
    ap.add_argument("--max-calls", type=int, default=60)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--only", help="only commits starting with this prefix")
    ap.add_argument("--keep-images", action="store_true")
    ap.add_argument("--retry-errors", action="store_true",
                    help="drop the healing rows of tasks with an ERROR run, then run them again")
    ap.add_argument("--summary", metavar="FILE")
    ap.add_argument("--per-task", action="store_true", help="with --summary: one line per task")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"),
                    help="runs whose outcome differs between two result files (replay check)")
    ap.add_argument("--copy-gold", metavar="FILE",
                    help="start --out with the passed gold runs (and unsupported-Python tasks) of FILE, "
                         "so a repeat pass (for example with warm model answers) skips them")
    a = ap.parse_args(argv)
    if a.compare:
        load = lambda p: {(r["commit"], r["setting"], r["run"]): r for r in (json.loads(x) for x in open(p) if x.strip())}  # noqa: E731
        A, B = load(a.compare[0]), load(a.compare[1])
        diff = [(k, A[k], B[k]) for k in sorted(A) if k in B and k[1] != "gold" and A[k]["outcome"] != B[k]["outcome"]]
        print(f"{sum(1 for k in A if k in B and k[1] != 'gold')} runs in both files, {len(diff)} differ")
        for k, x, y in diff:
            print(f"  {k[0]} {k[1]:12s} r{k[2]}: {x['outcome']} -> {y['outcome']}  statuses {x.get('statuses')} -> {y.get('statuses')}")
        return 0
    if a.summary:
        summary(a.summary, a.per_task, a.tier if a.tier != 'all' else None)
        return 0
    a.workdir = os.path.abspath(os.path.expanduser(a.workdir))
    a.cache_dir = os.path.join(R.ROOT, "results", "llm_cache_r2e")
    a.no_guard = False
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    done = set()
    if a.retry_errors and os.path.exists(a.out):
        rows = [json.loads(x) for x in open(a.out) if x.strip()]
        def broken(r):
            return r["outcome"] == "ERROR" or (r["outcome"] == "NOT_HEALABLE" and not r.get("statuses")
                                                and not r.get("skipped") and r["seconds"] < 3)
        bad = {r["commit"] for r in rows if broken(r)}
        keep = [r for r in rows if r["commit"] not in bad or (r["setting"] == "gold" and r["outcome"] == "PASSED")]
        with open(a.out, "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in keep)
        print(f"dropped the healing rows of {len(bad)} tasks that had an ERROR (gold rows that passed are kept)")
    if a.copy_gold and not os.path.exists(a.out):
        with open(a.out, "w") as f:
            for x in open(a.copy_gold):
                if x.strip():
                    d = json.loads(x)
                    if d["setting"] == "gold" or d["outcome"] == "UNSUPPORTED_PY":     # also failed gold runs
                        f.write(x if x.endswith("\n") else x + "\n")
    unsupported, bad_gold = set(), set()
    if os.path.exists(a.out):
        for x in open(a.out):
            if x.strip():
                d = json.loads(x)
                done.add((d["commit"], d["setting"], d["run"]))
                if d["outcome"] == "UNSUPPORTED_PY":
                    unsupported.add(d["commit"])
                if d["setting"] == "gold" and d["outcome"] != "PASSED":
                    bad_gold.add(d["commit"])          # does not pass with the gold patch: never evaluate
    tasks = pick_tasks(a.scan, a.n, a.with_threads,
                       [x for x in a.repos.split(",") if x] if a.repos else None, a.tier or "py39+")
    if a.only:
        tasks = [t for t in tasks if t["commit"].startswith(a.only)]
    settings = [s for s in a.settings.split(",") if s]
    print(f"{len(tasks)} tasks, settings {settings}, {a.runs} runs", flush=True)
    consecutive_bad = 0
    for t in tasks:
        if t["commit"] in unsupported or t["commit"] in bad_gold:
            continue
        row = R.load_row(t["commit"])
        sites = t["sites"]
        jobs = ([("gold", 1)] if (t["commit"], "gold", 1) not in done else []) + \
               [(s, r) for r in range(1, a.runs + 1) for s in settings if (t["commit"], s, r) not in done]
        for setting, run in jobs:
            rec = one(row, setting, run, sites, a, t.get("tier", "py39+"))
            with open(a.out, "a") as f:
                f.write(json.dumps(rec) + "\n")
            if rec["outcome"] == "UNSUPPORTED_PY":
                print(f"{t['commit']} {t['repo']}: repository Python is older than 3.9, task skipped")
                break
            if setting == "gold" and rec["outcome"] != "PASSED":
                print(f"{t['commit']} gold run is {rec['outcome']}: {rec.get('detail', rec['not_passing'])}",
                      flush=True)
                consecutive_bad += 1
                if consecutive_bad >= 3:
                    print("three tasks in a row without a passing gold run: stopping (Docker problem?)")
                    return 1
                break                      # a task that does not pass with the gold patch is unusable
            consecutive_bad = 0 if setting == "gold" else consecutive_bad
            print(f"{t['commit']} {t['repo']:8s} {setting:12s} r{run} {rec['outcome']:12s} "
                  f"{rec['seconds']:6.1f}s heals={rec.get('categories')} skipped={rec.get('skipped')}",
                  flush=True)
        if not a.keep_images:
            subprocess.run(["docker", "rmi", "-f", row["docker_image"]], capture_output=True)
    summary(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())