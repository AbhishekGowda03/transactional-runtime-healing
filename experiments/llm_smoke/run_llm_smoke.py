"""First runs with a real language model (smoke test, not a benchmark).

    uv run python experiments/llm_smoke/run_llm_smoke.py [--settings plain,full] [--out results/llm_smoke_02]
    ... --runs 3 --temperature 0.7        three independent samples of the model's repairs
    ... --prompt minimal                  the prompt without the "do not touch ..." rules

Settings come from a `.env` file (TXH_MODEL, TXH_API_BASE, ...; see .env.example) or the
environment. The runner loads `.env` itself, from the repository folder, and passes the values
to every run, so a changed model is never silently ignored.

For every program and setting the program runs with the model's repairs. The model is asked
once per distinct prompt (and run): answers go to a cache (results/llm_cache), so later
settings replay the same repairs and the settings are compared on identical repairs wherever
the prompts match. At temperature 0 a repeat would replay the same answers, so --runs > 1
needs a temperature above 0; every run has its own run id (part of the cache key and the seed).
The number of real model calls is capped across ALL processes of one runner start (--max-calls).

Per setting:
  proceeded      the program ran to its end (no "original error")
  correct        it proceeded AND printed an accepted state (see programs.py)
  by accident    correct, although a committed repair did not bind what the failing statement
                 would have bound (the repair did nothing; the printed state is a default)
  wrong result   it proceeded but printed a state that is not accepted (NOT the same as the
                 "harm" of StateLeak, which means state left behind by a failed repair)
  files changed  the run directory differs from the run without any repair
  damage         the program did NOT get through, yet its printed state or its directory differs
                 from the run without any repair: what a failed or rejected repair left behind
  residue        failed attempts that left state behind (plain mode with the meter; the live
                 protocol cannot, by construction)
  attempts       heal attempts and their outcomes; "no-op" = committed but did not bind the
                 failing statement's targets (or never set _eh_rtn for a return)
  model replies  ok / declined / not code / cut off / error, over all requests of the setting
Timing is not compared: the first setting that asks the model pays all the latency.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(__file__))
import programs as core_programs  # noqa: E402
import programs_shared  # noqa: E402

SETS = {"core": core_programs.PROGRAMS, "shared": programs_shared.PROGRAMS}


def accepted(p, state):
    """`ok` is a list of accepted printed states, or a function of the printed state."""
    if state is None:
        return False
    return p["ok"](state) if callable(p["ok"]) else state in p["ok"]

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
from transactional_runtime_healing.llm import load_dotenv  # noqa: E402

load_dotenv(ROOT)                      # before ENV is built, so the runs see the same settings
ENV = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))
MODULE = "transactional_runtime_healing.live_run"
OWN = {"prog.py", "log.jsonl", "none.json", "events.jsonl"}

# `report` is built with eval so that it is NOT part of the instrumented program: the runner's
# own printing must never be healed (it once was, and the "repair" was logged as committed).
TEMPLATE = """import os, sys
report = eval({lam!r})
sys.excepthook = lambda *a: (print("original error"), report())
{body}
report()
"""
SETTINGS = {
    "plain": ["--mode", "plain"],
    "plain+taint": ["--mode", "plain", "--guard", "taint"],
    "fork only": ["--mode", "live", "--no-guard"],
    "full": ["--mode", "live", "--post-commit", "block"],
    "full+targets": ["--mode", "live", "--post-commit", "block", "--require-targets"],
}
METERED = ("plain", "plain+taint")


def dir_hash(d):
    h = hashlib.sha1()
    for root, dirs, files in os.walk(d):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__")
        rel = os.path.relpath(root, d)
        if rel != ".":                                    # empty folders count too
            h.update(f"D:{rel}\n".encode())
        for name in sorted(files):
            if rel == "." and name in OWN:
                continue
            h.update(f"F:{os.path.join(rel, name)}\n".encode())
            with open(os.path.join(root, name), "rb") as f:
                h.update(f.read())
    return h.hexdigest()[:12]


def read_lines(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(x) for x in f.read().splitlines() if x.strip()]


def run(p, args, llm, timeout):
    """llm = None for the clean run (no repair), else dict(cache_dir, run_id, temperature, prompt)."""
    with tempfile.TemporaryDirectory() as d:
        for name, text in p["setup"].items():
            with open(os.path.join(d, name), "w") as f:
                f.write(text)
        lam = f"lambda: print('STATE', repr({p['state']}))"
        with open(os.path.join(d, "prog.py"), "w") as f:
            f.write(TEMPLATE.format(lam=lam, body=p["body"]))
        log = os.path.join(d, "log.jsonl")
        events = os.path.join(d, "events.jsonl")
        cmd = [sys.executable, "-m", MODULE, os.path.join(d, "prog.py"), "--log", log, *args]
        if llm is not None:
            cmd += ["--llm", "--cache-dir", llm["cache_dir"], "--llm-events", events,
                    "--temperature", str(llm["temperature"]), "--prompt", llm["prompt"]]
            if llm["run_id"]:
                cmd += ["--run-id", llm["run_id"]]
        else:
            with open(os.path.join(d, "none.json"), "w") as f:   # clean run: rules file, no rules
                f.write("[]")
            cmd += [os.path.join(d, "none.json")]
        t0 = time.monotonic()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, env=ENV, cwd=d, timeout=timeout)
            out, err = r.stdout, r.stderr
        except subprocess.TimeoutExpired:
            out, err = "", "timeout"
        secs = time.monotonic() - t0
        state = next((l[6:] for l in out.splitlines() if l.startswith("STATE ")), None)
        entries = [e for e in read_lines(log)
                   if e["status"] not in ("NO_PROPOSAL", "SKIPPED")
                   and not e["status"].startswith("post-commit")]
        return dict(state=state, proceeded="original error" not in out and state is not None,
                    entries=entries, replies=read_lines(events), dirhash=dir_hash(d), secs=secs,
                    err=err[-300:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--settings", default="plain,full", help="comma list of: " + ", ".join(SETTINGS))
    ap.add_argument("--cache-dir", default=os.path.join(ROOT, "results", "llm_cache"))
    ap.add_argument("--out", default=os.path.join(ROOT, "results", "llm_smoke_02"))
    ap.add_argument("--timeout", type=float, default=600.0, help="seconds per run (model calls included)")
    ap.add_argument("--runs", type=int, default=1, help="independent samples of the model's repairs")
    ap.add_argument("--temperature", type=float, default=None,
                    help="default 0 for one run, 0.7 for several")
    ap.add_argument("--prompt", choices=("rules", "minimal"), default="rules")
    ap.add_argument("--max-calls", type=int, default=300,
                    help="hard cap on real model calls, counted across all processes")
    ap.add_argument("--set", choices=sorted(SETS), default="core",
                    help="core = simple local repairs; shared = repairs that touch shared state, "
                         "the environment, files or other processes")
    ap.add_argument("--only")
    ns = ap.parse_args()
    temp = ns.temperature if ns.temperature is not None else (0.0 if ns.runs == 1 else 0.7)
    if ns.runs > 1 and temp == 0:
        ap.error("--runs > 1 at temperature 0 would repeat identical answers; give --temperature")
    names = [s.strip() for s in ns.settings.split(",")]
    for n in names:
        if n not in SETTINGS:
            ap.error(f"unknown setting {n!r}")
    progs = [p for p in SETS[ns.set] if not ns.only or ns.only in p["id"]]

    os.makedirs(ns.cache_dir, exist_ok=True)
    budget = os.path.join(ns.cache_dir, ".budget")
    with open(budget, "w") as f:                     # a fresh count for every runner start
        f.write("0")
    ENV["TXH_BUDGET_FILE"] = budget
    ENV["TXH_MAX_CALLS"] = str(ns.max_calls)
    print(f"set {ns.set}  model {ENV.get('TXH_MODEL', '(default)')}  prompt {ns.prompt}  temperature {temp}  "
          f"runs {ns.runs}  call cap {ns.max_calls}")

    clean = {}
    for p in progs:
        c = run(p, [], None, 60)
        clean[p["id"]] = (c["state"], c["dirhash"])
    rows = []
    for k in range(ns.runs):
        llm = dict(cache_dir=ns.cache_dir, run_id=str(k + 1) if ns.runs > 1 else "",
                   temperature=temp, prompt=ns.prompt)
        for name in names:
            print(f"[run ] {name}" + (f"  (run {k + 1})" if ns.runs > 1 else ""))
            for p in progs:
                args = list(SETTINGS[name]) + (["--meter"] if name in METERED else [])
                r = run(p, args, llm, ns.timeout)
                outcomes = [e["status"] for e in r["entries"]]
                noop = sum(1 for e in r["entries"]
                           if e["status"] == "COMMITTED" and e.get("targets_missing"))
                correct = r["proceeded"] and accepted(p, r["state"])
                files_changed = r["dirhash"] != clean[p["id"]][1]
                wrong = r["proceeded"] and not correct
                # damage after a failure: the program did not get through, yet its state or its
                # directory differs from the run without any repair (what a rejected or failed
                # repair left behind). This is the StateLeak meaning of "harm".
                damage = (not r["proceeded"]) and ((r["state"], r["dirhash"]) != clean[p["id"]])
                residue = sorted({x for e in r["entries"] if e["status"] != "COMMITTED"
                                  for x in (e.get("residue") or []) + (e.get("external_residue") or [])})
                rep = {}
                for e in r["replies"]:
                    rep[e["outcome"]] = rep.get(e["outcome"], 0) + 1
                model_error = bool(rep.get("error") or rep.get("unavailable"))
                verdict = ("correct" if correct and not noop else "correct (accident)" if correct
                           else "proceeded, wrong" if r["proceeded"] else "failed")
                rows.append(dict(run=k + 1, setting=name, program=p["id"], state=r["state"],
                                 proceeded=r["proceeded"], correct=correct,
                                 accident=bool(correct and noop), wrong=wrong, damage=damage,
                                 files_changed=files_changed, outcomes=outcomes, noop=noop,
                                 residue=residue, replies=rep, model_error=model_error,
                                 seconds=round(r["secs"], 1), stderr=r["err"]))
                print(f"   {p['id']:18} {verdict:19} "
                      f"{','.join(o.replace('REJECTED_', 'rej:').lower() for o in outcomes) or 'no repair':32}"
                      f" state={r['state']}{'  NO-OP x%d' % noop if noop else ''}"
                      f"{'  RESIDUE ' + str(residue) if residue else ''}"
                      f"{'  FILES' if files_changed else ''}{'  DAMAGE' if damage else ''}{'  MODEL ERROR' if model_error else ''}")

    os.makedirs(os.path.dirname(os.path.abspath(ns.out)), exist_ok=True)
    with open(ns.out + ".json", "w") as f:
        json.dump(dict(model=ENV.get("TXH_MODEL", "(default)"), prompt=ns.prompt, temperature=temp,
                       runs=ns.runs, rows=rows), f, indent=1)
    lines = ["| setting | proceeded | correct | by accident | wrong result | files changed | "
             "damage after failure | failed attempts that left residue | attempts (rejected, no-op) | "
             "model replies ok/declined/not code/cut off/error |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for name in names:
        rs = [r for r in rows if r["setting"] == name]
        n = len(rs)
        att = sum(len(r["outcomes"]) for r in rs)
        rej = sum(1 for r in rs for o in r["outcomes"] if o != "COMMITTED")
        rp = lambda key: sum(r["replies"].get(key, 0) for r in rs)
        err = rp("error") + rp("unavailable")
        lines.append(f"| {name} | {sum(r['proceeded'] for r in rs)}/{n} | {sum(r['correct'] for r in rs)}/{n} "
                     f"| {sum(r['accident'] for r in rs)} | {sum(r['wrong'] for r in rs)}/{n} "
                     f"| {sum(r['files_changed'] for r in rs)}/{n} "
                     f"| {sum(r['damage'] for r in rs)}/{n} "
                     f"| {sum(1 for r in rs if r['residue'])} programs "
                     f"| {att} ({rej} rejected, {sum(r['noop'] for r in rs)} no-op) "
                     f"| {rp('ok')}/{rp('decline')}/{rp('invalid')}/{rp('truncated')}/{err} |")
    table = "\n".join(lines)
    if any(r["model_error"] for r in rows):
        table += ("\n\nWARNING: some requests to the model failed (error / unavailable). "
                  "Those programs ran without a repair; do not read their rows as the model "
                  "declining.")
    with open(ns.out + ".md", "w") as f:
        f.write(table + "\n")
    print("\n" + table)


if __name__ == "__main__":
    main()