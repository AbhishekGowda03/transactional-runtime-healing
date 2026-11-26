"""Run the StateLeak cases in several settings and report the metrics.

    uv run python experiments/stateleak/run_stateleak.py [--repeat 3] [--out results/stateleak_02]

Settings (a setting is skipped, and said so, if the installed live_run lacks a flag it needs):
  plain            HealBench-style: the repair runs in the live process, nothing is undone
  plain+taint      plain + value-matching protected-operations check (HealGuard-inspired).
                   A HealGuard-inspired value-matching baseline (same guard as the protocol, no
                   fork); it does not implement HealGuard's taint analysis.
  fork only        the protocol (fork trial, validation point, effect blocking) with the
                   protected-operations check and the file undo switched off
  fork + file undo fork only + the filesystem journal (files of a rejected repair are restored)
  full, no file undo  the value-matching check (blocks after the commit as well), no file undo
  full protocol    everything: fork, validation, file undo and the value-matching check
                   (a HealGuard-inspired baseline, not HealGuard's taint analysis)

Metrics (per setting; 95% Clopper-Pearson intervals, which are exact for small counts):
  harm rate             among `bad` cases: damage reached the final state or the outside
                        world. Measured independently of the tool's own logs: the state the
                        program prints differs from the clean run, or the run directory
                        (content hash of every file and folder) differs from the clean run.
  unsafe-commit rate    among `bad` cases: the repair was committed (even if a later check
                        stopped the harm)
  residual-state rate   among rejected attempts (`bad` and `reject-only` cases): something is
                        left behind (state, directory, or residue / external residue in the
                        log). The denominator depends on how many attempts a setting rejects.
  false-rejection rate  among `good` cases: the repair was not committed, or the final state
                        is not the expected one
  time                  median seconds per case over timed runs (meter off) and the median
                        difference to the same program run without any repair (interpreter
                        start-up dominates these tiny programs, so only the difference means
                        anything, and it is far too small to support a claim about overhead)
Cases of the "limit-*" categories were written to find where the guard stops working; they
are reported in a separate table. The cases are hand-written and deterministic, so the
intervals describe this case set, not repairs in general.
The "clean" state of a case is measured, not written by hand: the program is run once with
no repair rules and the state it prints and the directory it leaves are the reference.
"""
import argparse
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(__file__))
from cases import CASES  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ENV = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))
MODULE = "transactional_runtime_healing.live_run"
OWN_FILES = {"prog.py", "rules.json", "log.jsonl"}

TEMPLATE = """import os, sys, json, random, warnings
START = os.getcwd()
def report():
    print("STATE", repr({state}))
sys.excepthook = lambda *a: (print("original error"), report())
{body}
report()
"""

# (name, base arguments, flags that must exist)
SETTINGS = [
    ("plain", ["--mode", "plain"], []),
    ("plain+taint", ["--mode", "plain", "--guard", "taint"], ["--guard"]),
    ("fork only", ["--mode", "live", "--no-guard", "--no-fs-isolation"], ["--no-fs-isolation"]),
    ("fork + file undo", ["--mode", "live", "--no-guard"], ["--no-fs-isolation"]),
    ("full, no file undo", ["--mode", "live", "--post-commit", "block", "--no-fs-isolation"],
     ["--post-commit", "--no-fs-isolation"]),
    ("full protocol", ["--mode", "live", "--post-commit", "block"],
     ["--post-commit", "--no-fs-isolation"]),
]


def dir_hash(d):
    """Content hash of every file and folder under d, except the runner's own files."""
    h = hashlib.sha1()
    for root, dirs, files in os.walk(d):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__")
        rel = os.path.relpath(root, d)
        if rel != ".":
            h.update(f"D:{rel}\n".encode())
        for name in sorted(files):
            if rel == "." and name in OWN_FILES:
                continue
            h.update(f"F:{os.path.join(rel, name)}\n".encode())
            with open(os.path.join(root, name), "rb") as f:
                h.update(f.read())
    return h.hexdigest()[:12]


PRE_INSTRUMENT = False
PYTHON_IMAGE = None                    # set by --python-image: run the programs under that image's Python
PRE_CACHE = None                       # where pre-instrumented programs go (Python before 3.9)


def python_command(workdir):
    """The command prefix that runs Python for one case, and the extra environment."""
    if not PYTHON_IMAGE:
        return [sys.executable], {}
    names = [k for k in ENV if k.startswith("TXH_")]
    # runs as root: the Python of some task images cannot load its own libraries as another user;
    # give_back() hands the files to the host user afterwards (the folder is deleted then)
    cmd = ["docker", "run", "--rm", "-v", f"{ROOT}/src:/txh:ro",
           "-v", f"{workdir}:{workdir}", "-v", f"{PRE_CACHE}:/precache", "-w", workdir,
           "-e", "HOME=/tmp", "-e", "PYTHONPATH=/txh", "-e", "PYTHONDONTWRITEBYTECODE=1",
           "-e", "TXH_PRE_CACHE=/precache"]
    for name in names:                    # by name: the value is read from the host's environment
        if name != "TXH_PRE_CACHE":
            cmd += ["-e", name]
    # timeout inside the container: killing the docker client alone would leave a hung program running
    cmd += [PYTHON_IMAGE, "timeout", "-s", "KILL", "55", "/testbed/.venv/bin/python"]
    return cmd, {}


def pre_instrument_program(path):
    """Instrument a case program on the host for a Python that cannot do it (before 3.9)."""
    from transactional_runtime_healing import pre_instrument, repo_hook
    with open(path, "rb") as f:
        raw = f.read()
    pre_instrument.write_entry(raw, path, PRE_CACHE, repo_hook._VERSION)


def give_back(workdir):
    """Files written by a container that ran as root: hand them to the host user."""
    if PYTHON_IMAGE:
        subprocess.run(["docker", "run", "--rm", "-v", f"{workdir}:{workdir}", PYTHON_IMAGE,
                        "chown", "-R", f"{os.getuid()}:{os.getgid()}", workdir],
                       capture_output=True)


def supported_flags():
    with tempfile.TemporaryDirectory() as d:
        cmd = python_command(d)[0]
        p = subprocess.run(cmd + ["-m", MODULE, "--help"], capture_output=True, text=True, env=ENV)
        give_back(d)
        if "--mode" not in p.stdout:
            sys.exit("live_run does not start with this Python:\n" + (p.stderr or p.stdout)[-1500:])
        return p.stdout


def run_case(case, args, rules, meter, timeout=60):
    """Run one case in a fresh directory. Returns a dict with state, status, log entry,
    seconds and the hash of the directory afterwards."""
    with tempfile.TemporaryDirectory() as d:
        for name, text in case["setup"].items():
            os.makedirs(os.path.dirname(os.path.join(d, name)) or d, exist_ok=True)
            with open(os.path.join(d, name), "w") as f:
                f.write(text)
        prog = os.path.join(d, "prog.py")
        with open(prog, "w") as f:
            f.write(TEMPLATE.format(state=case["state"], body=case["body"]))
        rul = os.path.join(d, "rules.json")
        with open(rul, "w") as f:
            json.dump(rules, f)
        log = os.path.join(d, "log.jsonl")
        py = python_command(d)[0]
        if PYTHON_IMAGE and PRE_INSTRUMENT:
            pre_instrument_program(prog)
        cmd = [*py, "-m", MODULE, prog, rul, "--log", log, *args, *case["args"]]
        if meter:
            cmd.append("--meter")
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, env=ENV, cwd=d,
                               timeout=timeout)
            out = p.stdout
        except subprocess.TimeoutExpired:
            out = ""
        secs = time.monotonic() - t0
        give_back(d)
        state = None
        for line in out.splitlines():
            if line.startswith("STATE "):
                state = line[6:]
        entries = []
        if os.path.exists(log):
            entries = [json.loads(x) for x in open(log).read().splitlines()]
        skipped = sorted({e.get("reason", "") for e in entries if e["status"] == "SKIPPED"})
        entries = [e for e in entries
                   if e["status"] not in ("NO_PROPOSAL", "SKIPPED")
                   and not e["status"].startswith("post-commit")]
        first = entries[0] if entries else None
        return dict(state=state, status=(first or {}).get("status", "NO_ATTEMPT"), entry=first,
                    secs=secs, dirhash=dir_hash(d), skipped=skipped)


def clopper_pearson(k, n, alpha=0.05):
    """Exact two-sided interval for a proportion k/n (bisection on the binomial tail)."""
    def cdf(x, p):                                   # P(X <= x)
        return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(x + 1))

    # lower: p where P(X >= k) = alpha/2 (rises with p); upper: p where P(X <= k) = alpha/2
    low = 0.0 if k == 0 else _bisect(lambda p: 1 - cdf(k - 1, p) - alpha / 2, increasing=True)
    high = 1.0 if k == n else _bisect(lambda p: cdf(k, p) - alpha / 2, increasing=False)
    return low, high


def _bisect(f, increasing):
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if (f(mid) < 0) == increasing:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def rate(flags):
    if not flags:
        return "n/a"
    k, n = sum(flags), len(flags)
    lo, hi = clopper_pearson(k, n)
    return f"{k}/{n} = {100 * k / n:.0f}% [{100 * lo:.0f}-{100 * hi:.0f}]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=3, help="timed runs per case and setting")
    ap.add_argument("--out", default=os.path.join(ROOT, "results", "stateleak_02"))
    ap.add_argument("--only", help="run only cases whose id contains this text")
    ap.add_argument("--allow-no-attempt", action="store_true",
                    help="do not stop when most cases of a setting had no repair attempt at all")
    ap.add_argument("--python-image", help="run the programs under the Python of this Docker image "
                    "(a task image such as namanjain12/numpy_final:<commit>, interpreter "
                    "/testbed/.venv/bin/python); with Python before 3.9 the programs are "
                    "pre-instrumented here first; use this for the Python 3.7 tier")
    ns = ap.parse_args()
    global PYTHON_IMAGE, PRE_CACHE, PRE_INSTRUMENT
    if ns.python_image:
        PYTHON_IMAGE = ns.python_image
        PRE_CACHE = tempfile.mkdtemp(prefix="txh-pre-")
        sys.path.insert(0, os.path.join(ROOT, "experiments", "healbench"))
        import preinstrument_image
        PRE_INSTRUMENT = preinstrument_image.python_of(PYTHON_IMAGE) < (3, 9)
        print(f"programs run under the Python of {PYTHON_IMAGE}: "
              f"{'%d.%d' % preinstrument_image.python_of(PYTHON_IMAGE)}"
              f"{' (pre-instrumented on the host)' if PRE_INSTRUMENT else ''}")
    cases = [c for c in CASES if not ns.only or ns.only in c["id"]]
    help_text = supported_flags()
    nb = lambda cs, lab: sum(c["label"] == lab for c in cs)
    print(f"{len(cases)} cases ({nb(cases, 'bad')} bad, {nb(cases, 'good')} good, "
          f"{nb(cases, 'reject-only')} reject-only)")

    reference, ref_time = {}, {}
    for c in cases:                                   # clean run: no repair at all
        runs = [run_case(c, ["--mode", "plain"], [], meter=False) for _ in range(ns.repeat)]
        reference[c["id"]] = (runs[0]["state"], runs[0]["dirhash"])
        ref_time[c["id"]] = statistics.median(r["secs"] for r in runs)

    rows = []
    stats = {}                                        # (setting, subset) -> lists
    for name, base, needs in SETTINGS:
        if any(flag not in help_text for flag in needs):
            print(f"[skip] {name}: this live_run has no {needs}")
            continue
        print(f"[run ] {name}")
        no_attempt, skip_reasons = 0, set()
        for c in cases:
            rules = [{"match": c["match"], "code": c["repair"]}]
            r = run_case(c, base, rules, meter=True)  # metered run: verdict only, not timed
            e = r["entry"] or {}
            committed = r["status"] == "COMMITTED"
            rejected = r["status"] not in ("COMMITTED", "NO_ATTEMPT")
            clean_state, clean_dir = reference[c["id"]]
            changed = r["state"] != clean_state or r["dirhash"] != clean_dir
            logged = bool(e.get("residue")) or bool(e.get("external_residue"))
            subset = ("limits" if c["cat"].startswith("limit") else
                      "undetected" if c["cat"].startswith("undetected") else "core")
            s = stats.setdefault((name, subset), dict(residual=[], unsafe=[], harm=[],
                                                      falserej=[], times=[], extra=[]))
            if rejected and c["label"] in ("bad", "reject-only"):
                s["residual"].append(int(changed or logged))
            if c["label"] == "bad":
                s["unsafe"].append(int(committed))
                s["harm"].append(int(changed))
                verdict = ("HARM" if changed else "COMMITTED, NO HARM" if committed
                           else "LOGGED RESIDUE" if logged else "clean")
            elif c["label"] == "good":
                wrong = (not committed) or r["state"] != c["expect"]
                s["falserej"].append(int(wrong))
                verdict = "FALSE REJECTION" if wrong else "ok"
            else:
                verdict = "RESIDUE" if (rejected and (changed or logged)) else (
                    "not rejected" if not rejected else "clean")
            ts = [run_case(c, base, rules, meter=False)["secs"] for _ in range(ns.repeat)]
            s["times"].append(statistics.median(ts))
            s["extra"].append(statistics.median(ts) - ref_time[c["id"]])
            rows.append(dict(setting=name, case=c["id"], cat=c["cat"], label=c["label"],
                             status=r["status"], state=r["state"], clean=clean_state,
                             dir_changed=r["dirhash"] != clean_dir, verdict=verdict,
                             residue=e.get("residue") or [],
                             external_residue=e.get("external_residue") or [],
                             seconds=round(s["times"][-1], 3)))
            print(f"   {c['id']:36} {c['label']:11} {r['status']:30} {verdict}")
            no_attempt += r["status"] == "NO_ATTEMPT"
            skip_reasons.update(r["skipped"])
        # A missing attempt looks like a safe outcome (no harm), so a setting in which most cases were
        # never tried is not a result: stop instead of scoring it.
        if no_attempt * 2 >= len(cases) and not ns.allow_no_attempt:
            sys.exit(f"\nINVALID: {no_attempt} of {len(cases)} cases had no repair attempt in '{name}'. "
                     f"The protocol did not run, so the harm and false-rejection rates would mean nothing.\n"
                     f"Reasons the healer gave for skipping: {sorted(skip_reasons) or 'none logged'}\n"
                     f"Nothing was written. (--allow-no-attempt overrides this check.)")

    os.makedirs(os.path.dirname(os.path.abspath(ns.out)), exist_ok=True)
    with open(ns.out + ".json", "w") as f:
        json.dump(rows, f, indent=1)
    tables = []
    for subset, title in (("core", "Core cases"), ("limits", "Limit cases (written to find "
                                                 "where the guard stops working)"),
                          ("undetected", "Operations the Python 3.7 wrappers do not notice "
                                         "(audit hooks notice them)")):
        lines = [f"### {title}", "",
                 "| setting | harm rate (bad) | unsafe-commit rate (bad) | residual-state rate "
                 "(rejected) | false-rejection rate (good) | median time/case | median extra "
                 "vs no repair |", "|---|---|---|---|---|---|---|"]
        for name, *_ in SETTINGS:
            s = stats.get((name, subset))
            if not s:
                continue
            lines.append(f"| {name} | {rate(s['harm'])} | {rate(s['unsafe'])} | "
                         f"{rate(s['residual'])} | {rate(s['falserej'])} | "
                         f"{statistics.median(s['times']):.3f}s | "
                         f"{statistics.median(s['extra']) * 1000:+.0f} ms |")
        tables.append("\n".join(lines))
    table = "\n\n".join(tables)
    with open(ns.out + ".md", "w") as f:
        f.write(table + "\n")
    print("\n" + table)


if __name__ == "__main__":
    main()