"""Run the tests of one R2E-Gym-Lite task inside its Docker image, with or without healing.

    uv run --with datasets python experiments/r2e/run_r2e.py --commit <hash> --settings gold,off,plain,live

  gold   the files of the real fix are copied over the repository first (all tests must pass,
         otherwise the setup is wrong)
  off    the repository as it is in the image (the base commit); the failing tests and the
         places where they crash are taken from this run
  plain  HealBench-style: the model's repair is applied, nothing is undone
  live   the protocol: the repair runs in a forked child and is committed only if the checks pass

The tests come from the dataset (`test_file_codes`) and are mounted into the container as
/testbed/r2e_tests; the image is pulled by docker when missing. Healing runs inside the
container (our package is mounted read-only). The model settings (TXH_*) are read from your
.env on the host and handed to the container by NAME, so the key is never printed or stored.
Repairs are only requested while a test method runs and only at the crash sites of the failing
`off` run (--heal-anywhere lifts that). Model answers are cached (results/llm_cache_r2e).
One result line per setting is appended to results/r2e/healed.jsonl.
Outcome: PASSED (all tests the dataset says pass at the fix do pass), FAILED (some do not),
ERROR (nothing ran), MODEL_ERROR (the model could not be reached), NOT_APPLICABLE (no crash
site in repository code), NOT_HEALABLE (the tests still fail and no heal point was ever reached
at a crash site: the method was not applied, so the task does not count against it).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))


def load_row(commit, dataset="R2E-Gym/R2E-Gym-Lite", split="train"):
    from datasets import load_dataset
    for r in load_dataset(dataset, split=split):
        if r["commit_hash"].startswith(commit):
            return r
    raise SystemExit("commit not found in the dataset")


def _obj(v):
    """Some dataset fields are JSON text, some are already parsed."""
    return json.loads(v) if isinstance(v, (str, bytes)) else v


def is_test_path(p):
    return bool(re.search(r"(^|/)(tests?|testing)(/|$)|(^|/)test_[^/]*\.py$|_tests?\.py$", p))


def write_inputs(row, folder, setting):
    """Test files and (for gold) the fixed source files, as plain files for mounting."""
    ex = _obj(row["execution_result_content"])
    tdir = os.path.join(folder, "r2e_tests")
    os.makedirs(tdir, exist_ok=True)
    for name, code in zip(ex["test_file_names"], ex["test_file_codes"]):
        with open(os.path.join(tdir, name), "w") as f:
            f.write(code)
    gdir = os.path.join(folder, "gold")
    os.makedirs(gdir, exist_ok=True)
    copied = []
    if setting == "gold":
        for fd in _obj(row["parsed_commit_content"])["file_diffs"]:
            path = _obj(fd["header"])["file"]["path"]
            if path.endswith(".py") and not is_test_path(path):
                dest = os.path.join(gdir, path)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "w") as f:
                    f.write(fd["new_file_content"])
                copied.append(path)
    return tdir, gdir, copied


_HEADER = r"^_{3,} (.+?) _{3,}$\n(.*?)(?=^_{3,} .+? _{3,}$|^={3,}|\Z)"   # a failure section


def test_style(row):
    """"runner": a custom unittest runner script ships with the tests (tornado);
    "pytest": the task is started with pytest -rA r2e_tests (all other repositories)."""
    names = _obj(row["execution_result_content"])["test_file_names"]
    return "runner" if any(n.endswith("_runner.py") for n in names) else "pytest"


def parse_report(text):
    """Test results: {'Class.test': (status, reason)} (names as in expected_output_json)."""
    out = {}
    for m in re.finditer(r"^(PASSED|FAILED|ERROR|SKIPPED) (\S+?::\S+?)(?: - (.*))?$", text, re.M):
        status, node, why = m.groups()                      # custom runner: test_1::Class::test
        if ".py::" not in node:
            out[".".join(node.split("::")[1:]) or node] = (status, (why or "")[:120])
    for m in re.finditer(r"^(PASSED|FAILED|ERROR|XFAIL|XPASS) (\S+\.py::.*?)(?: - (.*))?$", text, re.M):
        status, node, why = m.groups()                      # pytest -rA: r2e_tests/test_1.py::Class::test[p]
        name = ".".join(node.split("::")[1:])
        if name:
            out[name] = ("PASSED" if status == "XPASS" else status, (why or "")[:120])
    reasons = {}                                    # last traceback line of each failed test
    for m in re.finditer(_HEADER, text, re.M | re.S):
        lines = [l for l in m.group(2).strip().splitlines() if l.strip()]
        if lines:
            reasons[m.group(1)] = lines[-1][:120]
    return {k: (v[0], v[1] or reasons.get(k, "")) for k, v in out.items()}


def crash_sites(text, repo_root="/testbed", which="below-test"):
    """`file:line` of the repository frames in the traceback of every failure.

    below-test (default): only frames AFTER the test's own frame, i.e. the code the test called.
        Frames before it belong to the test machinery (runners, loop helpers that run the test
        coroutine); repairing there only hides the failure of the whole test.
    all:  every repository frame outside test files.
    last: only the innermost frame (where the exception was raised; wrong when the program
        catches it and hands it on through a future)."""
    sites = []
    for m in re.finditer(_HEADER, text, re.M | re.S):
        frames, seen_test = [], False
        for f in re.finditer(r'File "([^"]+)", line (\d+)', m.group(2)):
            path = f.group(1)
            if not path.startswith(repo_root + "/") or "/.venv/" in path:
                continue
            rel = path[len(repo_root) + 1:]
            if is_test_path(rel) or rel.startswith("r2e_tests/"):
                seen_test = True
                continue
            if which != "below-test" or seen_test:
                frames.append(f"{rel}:{f.group(2)}")
        for site in (frames[-1:] if which == "last" else frames):
            if site not in sites:
                sites.append(site)
    return sites


def dotenv_names():
    """TXH_* settings from the host's .env, handed to the container by NAME (value-less -e)."""
    from transactional_runtime_healing.llm import load_dotenv
    load_dotenv(ROOT)
    return sorted(k for k in os.environ if k.startswith("TXH_"))


# Evaluation settings. "live" is kept as it was (live mode, default options).
HEAL_SETTINGS = {
    "plain": dict(TXH_HEAL_MODE="plain"),                                  # HealBench-style, no fork
    "plain+guard": dict(TXH_HEAL_MODE="plain", TXH_HEAL_GUARD="taint"),    # value-matching baseline
    "fork-only": dict(TXH_HEAL_MODE="live", TXH_HEAL_NO_GUARD="1", TXH_HEAL_NO_FS_ISOLATION="1"),
    "full": dict(TXH_HEAL_MODE="live", TXH_HEAL_POST_COMMIT="block"),
    "live": dict(TXH_HEAL_MODE="live"),
}


def run_container(row, setting, a, sites):
    commit = row["commit_hash"]
    run_tag = "" if str(a.run_id) in ("1", "") else f"_r{a.run_id}"
    folder = os.path.join(a.workdir, f"{commit[:12]}_{setting}{run_tag}")
    os.makedirs(folder, exist_ok=True)
    out = os.path.join(folder, "out")
    os.makedirs(out, exist_ok=True)
    for n in ("heal.jsonl", "events.jsonl", "stats.json", "budget"):
        if os.path.exists(os.path.join(out, n)):
            os.remove(os.path.join(out, n))
    tdir, gdir, copied = write_inputs(row, folder, setting)
    hook_cache = os.path.join(a.workdir, f"hookcache_{row['repo_name']}")
    llm_cache = os.path.abspath(a.cache_dir)
    for d in (hook_cache, llm_cache):
        os.makedirs(d, exist_ok=True)
    healing = setting in HEAL_SETTINGS
    cmd = ["docker", "run", "--rm", "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
           "-e", "PYTHONHASHSEED=0",                  # the same prompts and test behaviour in every process
           "-e", "PYTHONPATH=/txh:/testbed", "-v", f"{tdir}:/testbed/r2e_tests:ro",
           "-v", f"{gdir}:/gold:ro", "-v", f"{os.path.join(ROOT, 'src')}:/txh:ro",
           "-v", f"{out}:/out", "-v", f"{hook_cache}:/hookcache", "-v", f"{llm_cache}:/llmcache",
           "-w", "/testbed"]
    if healing:
        for name in dotenv_names():                   # by name: the value never appears here
            cmd += ["-e", name]
        env = dict(TXH_HEAL_ROOT="/testbed", TXH_HEAL_LOG="/out/heal.jsonl",
                   TXH_HEAL_CACHE="/hookcache", TXH_LLM_EVENTS="/out/events.jsonl",
                   TXH_HEAL_STATS="/out/stats.json", TXH_CACHE_DIR="/llmcache",
                   TXH_MAX_CALLS=str(a.max_calls), TXH_BUDGET_FILE="/out/budget",
                   TXH_RUN_ID=a.run_id)
        env.update(HEAL_SETTINGS[setting])
        if getattr(a, "temperature", None) is not None:
            env["TXH_TEMPERATURE"] = str(a.temperature)
        if sites:
            env["TXH_HEAL_ONLY"] = ";".join(sites)
        if setting in ("live", "full") and a.no_guard:
            env["TXH_HEAL_NO_GUARD"] = "1"
        if setting in ("live", "full", "fork-only") and a.pool_threads:
            env["TXH_HEAL_POOL_THREADS"] = "1"
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        heal_args = True
    else:
        heal_args = False
    if test_style(row) == "runner":
        py = ("-m transactional_runtime_healing.unittest_heal tornado_unittest_runner.py"
              if heal_args else "tornado_unittest_runner.py")
        run = f"cd /testbed/r2e_tests && /testbed/.venv/bin/python -W ignore {py}"
    else:
        extra = "-p transactional_runtime_healing.pytest_plugin -s " if heal_args else ""
        run = ("cd /testbed && PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' "
               f"/testbed/.venv/bin/python -W ignore -m pytest -rA --tb=native "
               f"-p no:cacheprovider $NOCOV {extra}r2e_tests")
    # the files written into the mounted folders are handed back to the host user
    # pytest-cov (aiohttp, ...) breaks in forked children (coverage's atexit assertion), so it is
    # switched off for every setting; the option exists only when the plugin is installed
    script = (f"cp -r /gold/. /testbed/ 2>/dev/null; "
              f"NOCOV=$(/testbed/.venv/bin/python -c 'import pytest_cov' 2>/dev/null && echo --no-cov); "
              f"{run}; "
              f"rc=$?; chown -R {os.getuid()}:{os.getgid()} /out /hookcache /llmcache 2>/dev/null; "
              f"exit $rc")
    cmd += [row["docker_image"], "bash", "-lc", script]
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=a.timeout)
        text, code = p.stdout + "\n" + p.stderr, p.returncode
    except subprocess.TimeoutExpired:
        text, code = "timeout", -1
    with open(os.path.join(folder, "pytest.txt"), "w") as f:
        f.write(text)
    return text, code, copied, out, round(time.monotonic() - t0, 1)


def judge(row, text, code):
    got = parse_report(text)
    expected = _obj(row["expected_output_json"])
    counts = {}
    for st, _ in got.values():
        counts[st] = counts.get(st, 0) + 1
    bad = {k: got.get(k, ("MISSING", "")) for k, v in expected.items()
           if v == "PASSED" and got.get(k, ("",))[0] != "PASSED"}
    if not got:
        outcome = "ERROR"
    else:
        outcome = "FAILED" if bad else "PASSED"
    return outcome, counts, bad


def records_at_sites(out, sites):
    """Heal records (any status) whose statement covers a crash site: {status: count}."""
    hp = os.path.join(out, "heal.jsonl")
    found = {}
    if not (sites and os.path.exists(hp)):
        return found
    wanted = []
    for site in sites:
        f, _, n = site.rpartition(":")
        wanted.append((f, int(n)))
    for line in open(hp):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        last = d["line"] + d["statement"].count("\n")
        if any(d["file"].endswith(f) and d["line"] <= n <= last for f, n in wanted):
            found[d["status"]] = found.get(d["status"], 0) + 1
    return found


def summarize_healing(out):
    res = {}
    hp = os.path.join(out, "heal.jsonl")
    if os.path.exists(hp):
        heals = {}
        for line in open(hp):
            try:
                st = json.loads(line)["status"]
            except ValueError:
                continue
            if st not in ("SKIPPED", "NO_PROPOSAL"):
                heals[st] = heals.get(st, 0) + 1
        res["heals"] = heals
    ep = os.path.join(out, "events.jsonl")
    if os.path.exists(ep):
        ev = [json.loads(x) for x in open(ep) if x.strip()]
        res["model"] = dict(requests=len(ev), failed=sum(e.get("outcome") in ("error", "unavailable")
                                                          for e in ev),
                            from_cache=sum(e.get("source") == "cache" for e in ev),
                            from_model=sum(e.get("source") == "model" for e in ev))
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--commit", required=True)
    ap.add_argument("--settings", default="gold,off")
    ap.add_argument("--workdir", default=os.path.expanduser("~/r2e_work"))
    ap.add_argument("--cache-dir", default=os.path.join(ROOT, "results", "llm_cache_r2e"))
    ap.add_argument("--out", default=os.path.join(ROOT, "results", "r2e", "healed.jsonl"))
    ap.add_argument("--max-calls", type=int, default=40)
    ap.add_argument("--run-id", default="1")
    ap.add_argument("--heal-anywhere", action="store_true")
    ap.add_argument("--sites", choices=("below-test", "all", "last"), default="below-test",
                    help="where repairs are allowed: frames the test called (default), every "
                         "repository frame, or only where the exception was raised")
    ap.add_argument("--pool-threads", action="store_true",
                    help="live: fork although idle thread-pool workers exist (experimental)")
    ap.add_argument("--no-guard", action="store_true")
    ap.add_argument("--timeout", type=int, default=1800)
    a = ap.parse_args(argv)
    a.workdir = os.path.abspath(os.path.expanduser(a.workdir))
    row = load_row(a.commit)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    settings = [s.strip() for s in a.settings.split(",") if s.strip()]
    sites = None
    if any(s in HEAL_SETTINGS for s in settings) and not a.heal_anywhere:
        text, code, _, _, _ = run_container(row, "off", a, None)
        sites = crash_sites(text, which=a.sites)
        print("crash sites of the failing run:", sites or "none in repository code")
    for setting in settings:
        res = dict(commit=a.commit, repo=row["repo_name"], setting=setting,
                   time=time.strftime("%Y-%m-%d %H:%M:%S"), sites=sites)
        if setting in HEAL_SETTINGS and not a.heal_anywhere and not sites:
            res.update(outcome="NOT_APPLICABLE", detail="no crash in repository code")
        else:
            text, code, copied, out, secs = run_container(row, setting, a, sites)
            outcome, counts, bad = judge(row, text, code)
            res.update(outcome=outcome, counts=counts, seconds=secs, gold_files=copied,
                       not_passing={k: v[1] for k, v in list(bad.items())[:10]},
                       **summarize_healing(out))
            if setting in HEAL_SETTINGS and sites:
                res["at_sites"] = records_at_sites(out, sites)
                if outcome != "PASSED" and not res["at_sites"]:
                    res["outcome"] = "NOT_HEALABLE"
                    res["detail"] = ("no heal point was reached at any crash site (an explicit "
                                     "raise, or the program handles the exception itself)")
            if res.get("model", {}).get("failed"):
                res["detail"] = f"outcome was {outcome}, but the model could not be reached"
                res["outcome"] = "MODEL_ERROR"
            if outcome == "ERROR":
                res["detail"] = text.strip()[-300:].replace("\n", " | ")
        with open(a.out, "a") as f:
            f.write(json.dumps(res) + "\n")
        print(f"{setting:6s} {res['outcome']:14s} {res.get('counts')} heals={res.get('heals')} "
              f"model={res.get('model')} not_passing={res.get('not_passing')}")
    print("results:", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())