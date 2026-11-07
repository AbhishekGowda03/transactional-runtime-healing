"""Run the failing tests of SWE-bench instances with runtime healing (needs a model).

    uv run --with pandas --with pyarrow python experiments/swebench/run_healed.py \
        --parquet swe_lite_test.parquet --ids sympy__sympy-13480,sympy__sympy-21171

Settings, one pass each (default: off,plain,live):
  gold   the official fix applied (check: the tests must pass, else the setup is wrong)
  off    the tests as they are (reference: should fail; also gives the crash sites)
  plain  HealBench-style: the model's repair is applied, nothing is undone
  live   the protocol: the repair runs in a forked child and is committed only if the checks pass
The model comes from your .env (TXH_MODEL, TXH_API_BASE, TXH_API_KEY ...); this script never
prints or stores the key. Repairs are only requested while a test body is running and only for exceptions raised at the
crash sites of the failing `off` run (--heal-anywhere lifts that), and at most
--max-calls model requests are made per instance and setting (answers are cached in
--cache-dir, so a repeated run costs nothing).

Needs the work folder made by classify.py (clone and virtual environment in --workdir).
The FIRST run on a repository rewrites every module it imports (sympy: about 15 minutes); the
rewritten code is cached in --workdir/heal_cache, so later runs take seconds to minutes.

What is measured for each instance and setting (one line in results/swebench/healed_<repo>.jsonl):
  outcome   PASSED (the tests that must now pass do pass), FAILED_ASSERTION (the crash is gone
            but the result is wrong), FAILED_EXCEPTION (still crashes), ERROR (nothing ran)
  heals     how many repairs were committed, rejected, or not offered a repair
  model     requests, cache hits, declined/invalid replies
Not measured yet: whether tests that passed before still pass (PASS_TO_PASS).
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import classify  # noqa: E402
from transactional_runtime_healing.llm import load_dotenv  # noqa: E402


def _git(repo, *args, **kw):
    return classify.run(["git", *args], cwd=repo, **kw)


def crash_sites(tests, repo):
    """`file:line` where the failing tests raised, when that was repository code."""
    sites = []
    for t in tests:
        if t.get("outcome") == "failed" and t.get("raised_in_repo_code") and t.get("raised_at"):
            if t["raised_at"] not in sites:
                sites.append(t["raised_at"])
    return sites


def run_one(row, setting, a, py, repo, recipe_dir, sites=None):
    iid = row["instance_id"]
    out = dict(instance_id=iid, setting=setting, time=time.strftime("%Y-%m-%d %H:%M:%S"))
    _git(repo, "reset", "--hard", "-q")
    _git(repo, "clean", "-fdxq")
    r = _git(repo, "checkout", "-q", "-f", row["base_commit"], timeout=1800)
    if r.returncode:
        return dict(out, outcome="ERROR", detail="checkout failed")
    r = _git(repo, "apply", "--whitespace=nowarn", "-", input_text=row["test_patch"])
    if r.returncode:
        return dict(out, outcome="ERROR", detail="test patch does not apply")
    if setting == "gold":
        r = _git(repo, "apply", "--whitespace=nowarn", "-", input_text=row["patch"])
        if r.returncode:
            return dict(out, outcome="ERROR", detail="official patch does not apply")
    f2p = classify._tests(row["FAIL_TO_PASS"])
    names = {t.split("::")[-1].split("[")[0] for t in f2p}
    work = os.path.join(a.out_dir, "work", f"{iid}_{setting}")
    os.makedirs(work, exist_ok=True)
    paths = {k: os.path.join(work, k) for k in ("results.jsonl", "heal.jsonl", "events.jsonl",
                                                "stats.json", "pytest.txt", "budget")}
    for p in paths.values():
        if os.path.exists(p):
            os.remove(p)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", CLASSIFY_OUT=paths["results.jsonl"],
               CLASSIFY_ROOT=repo,
               PYTHONPATH=os.pathsep.join([repo, HERE, os.path.join(ROOT, "src")]))
    cmd = [py, "-m", "pytest", "-p", "classify_plugin", "-p", "no:cacheprovider", "-q", "-s",
           "-k", " or ".join(sorted(names)), *classify.test_files(row["test_patch"])]
    if setting in ("plain", "live"):
        if not a.heal_anywhere:
            if not sites:
                return dict(out, outcome="NOT_APPLICABLE",
                            detail="the failing run has no crash in repository code")
            env["TXH_HEAL_ONLY"] = ";".join(sites)
        env.update(TXH_HEAL_ROOT=repo, TXH_HEAL_MODE=setting, TXH_HEAL_LOG=paths["heal.jsonl"],
                   TXH_HEAL_CACHE=os.path.join(a.workdir, "heal_cache"),
                   TXH_LLM_EVENTS=paths["events.jsonl"], TXH_HEAL_STATS=paths["stats.json"],
                   TXH_CACHE_DIR=os.path.abspath(a.cache_dir), TXH_MAX_CALLS=str(a.max_calls),
                   TXH_BUDGET_FILE=paths["budget"], TXH_RUN_ID=a.run_id)
        if a.require_targets:
            env["TXH_HEAL_REQUIRE_TARGETS"] = "1"
        if setting == "live" and a.no_guard:
            env["TXH_HEAL_NO_GUARD"] = "1"
        cmd[3:3] = ["-p", "transactional_runtime_healing.pytest_plugin"]
    t0 = time.monotonic()
    try:
        p = classify.run(cmd, cwd=repo, env=env, timeout=a.timeout)
        text, code = p.stdout + p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        text, code = f"timeout after {a.timeout}s\n{(e.stdout or b'')[-500:]!r}", -1
    out["seconds"] = round(time.monotonic() - t0, 1)
    with open(paths["pytest.txt"], "w") as f:
        f.write(text)
    _git(repo, "reset", "--hard", "-q")
    _git(repo, "clean", "-fdxq")

    recs = []
    if os.path.exists(paths["results.jsonl"]):
        recs = [json.loads(x) for x in open(paths["results.jsonl"]) if x.strip()]
    mine = [r for r in recs if r["name"].split("[")[0] in names and r.get("when", "call") == "call"]
    out["tests"] = [dict(name=r["name"], outcome=r["outcome"], exc_type=r.get("exc_type"))
                    for r in mine]
    if not mine:
        out.update(outcome="ERROR", detail=text[-300:].replace("\n", " | "))
    elif all(r["outcome"] == "passed" for r in mine) and len(mine) >= len(names):
        out["outcome"] = "PASSED"
    elif any(r.get("exc_type") not in (None, "AssertionError") for r in mine
             if r["outcome"] == "failed"):
        out["outcome"] = "FAILED_EXCEPTION"
    else:
        out["outcome"] = "FAILED_ASSERTION"

    heals = {}
    if os.path.exists(paths["heal.jsonl"]):
        for line in open(paths["heal.jsonl"]):
            try:
                st = json.loads(line)["status"]
            except ValueError:
                continue
            heals[st] = heals.get(st, 0) + 1
    out["heals"] = {k: v for k, v in heals.items() if k != "SKIPPED"}
    if os.path.exists(paths["events.jsonl"]):
        ev = [json.loads(x) for x in open(paths["events.jsonl"]) if x.strip()]
        kinds = {}
        for e in ev:
            k = f"{e.get('source', '?')}:{e.get('outcome', '?')}"
            kinds[k] = kinds.get(k, 0) + 1
        out["model"] = dict(requests=len(ev), kinds=kinds)
        if any(e.get("outcome") in ("error", "unavailable") for e in ev):
            out["detail"] = f"outcome was {out['outcome']}, but the model could not be reached"
            out["outcome"] = "MODEL_ERROR"
    out["logs"] = work
    out["sites"] = sites
    if setting == "off":
        out["crash_sites"] = crash_sites(mine, repo)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default="sympy/sympy")
    ap.add_argument("--ids", help="comma-separated instance ids (default: five chosen sympy ones)")
    ap.add_argument("--parquet")
    ap.add_argument("--workdir", default=os.path.expanduser("~/swe_work"))
    ap.add_argument("--settings", default="gold,off,plain,live")
    ap.add_argument("--heal-anywhere", action="store_true",
                    help="repair exceptions at any site, not only the failing run's crash sites")
    ap.add_argument("--out-dir", default="results/swebench")
    ap.add_argument("--cache-dir", default="results/llm_cache_swe")
    ap.add_argument("--max-calls", type=int, default=40, help="model requests per instance and setting")
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--run-id", default="1", help="change it to get fresh model answers")
    ap.add_argument("--require-targets", action="store_true")
    ap.add_argument("--no-guard", action="store_true", help="live without the protected-operations check")
    a = ap.parse_args(argv)
    # pytest runs inside the repository clone, so every path it gets must be absolute
    a.workdir = os.path.abspath(os.path.expanduser(a.workdir))
    a.out_dir = os.path.abspath(a.out_dir)
    a.cache_dir = os.path.abspath(a.cache_dir)
    load_dotenv()
    if not os.environ.get("TXH_MODEL") and "off" != a.settings:
        print("note: TXH_MODEL is not set; the default local model is used (see .env.example)")
    default = "sympy__sympy-13471,sympy__sympy-13480,sympy__sympy-18621,sympy__sympy-21171,sympy__sympy-23117"
    ids = [x.strip() for x in (a.ids or default).split(",") if x.strip()]
    rows = {r["instance_id"]: r for r in classify.load_rows(a.parquet) if r["repo"] == a.repo}
    recipe = classify.RECIPES[a.repo]
    py = classify.ensure_venv(a.workdir, recipe)
    repo = classify.ensure_repo(a.workdir, a.repo)
    os.makedirs(a.out_dir, exist_ok=True)
    path = os.path.join(a.out_dir, f"healed_{a.repo.replace('/', '__')}.jsonl")
    for iid in ids:
        if iid not in rows:
            print(f"{iid}: not in the dataset")
            continue
        settings = a.settings.split(",")
        sites = None
        if any(x in settings for x in ("plain", "live")) and not a.heal_anywhere:
            base = run_one(rows[iid], "off", a, py, repo, a.workdir)       # finds the crash sites
            sites = base.get("crash_sites") or []
        for setting in settings:
            res = run_one(rows[iid], setting, a, py, repo, a.workdir, sites)
            with open(path, "a") as f:
                f.write(json.dumps(res) + "\n")
            print(f"{iid:22s} {setting:6s} {res['outcome']:17s} heals={res.get('heals')} "
                  f"model={res.get('model', {}).get('requests', 0)} [{res.get('seconds')}s]")
    print("results:", path)


if __name__ == "__main__":
    main()