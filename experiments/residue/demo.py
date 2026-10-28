"""Residual state after failed repairs: plain healing vs the protocol (first look at RQ1/RQ2).

Each program is run twice with the same repair rules, once in plain mode (HealBench-style,
no undo) and once in live mode (the protocol), with the meter on.

Run:  uv run python experiments/residue/demo.py
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap

HOOK = "import sys\nsys.excepthook = lambda *a: print('  program ended with the original error')\n"

CASES = [
    ("repair changes a shared list, then raises", """
        shared = [1, 2, 3]
        def f():
            v = 1 / 0
        f()
        print('shared =', shared)
     """, "shared = [1, 2, 3]", "shared.append(99)\nraise ValueError('bad')", []),
    ("repair changes a shared dict, then hangs (timeout 1 s)", """
        config = {'retries': 3}
        def f():
            v = 1 / 0
        f()
     """, "config = {'retries': 3}", "config['retries'] = 0\nwhile True: pass", ["--timeout", "1"]),
    ("repair sets an environment variable, then raises", """
        import os
        def f():
            v = 1 / 0
        f()
     """, "environment", "import os\nos.environ['HEAL_DEMO'] = '1'\nraise ValueError('bad')", []),
    ("repair writes a cache file, then raises", """
        def f():
            v = 1 / 0
        f()
     """, "", "open('heal_cache.txt', 'w').write('x')\nraise ValueError('bad')", []),
    ("repair patches a library setting, then raises", """
        import json
        def f():
            v = 1 / 0
        f()
     """, "", "import json\njson.loads = float\nraise ValueError('bad')", []),
    ("repair runs a command (os.system), then raises", """
        def f():
            v = 1 / 0
        f()
     """, "", "import os\nos.system('true')\nraise ValueError('bad')", []),
    ("repair silences warnings and fixes the random seed, then raises", """
        def f():
            v = 1 / 0
        f()
     """, "", "import warnings, random\nwarnings.simplefilter('ignore')\nrandom.seed(0)\nraise ValueError('bad')", []),
    ("good repair that also changes a shared list", """
        log = []
        def f():
            v = 1 / 0
            return v
        print('result', f(), 'log =', log)
     """, "", "log.append('healed')\nv = 0", []),
]


def run(mode, program, code, extra, env, cwd):
    prog, rul, log = (os.path.join(cwd, n) for n in (f"{mode}.py", f"{mode}.json", f"{mode}.jsonl"))
    open(prog, "w").write(HOOK + textwrap.dedent(program))
    json.dump([{"match": "1 / 0", "code": code}], open(rul, "w"))
    p = subprocess.run([sys.executable, "-m", "transactional_runtime_healing.live_run", prog, rul,
                        "--mode", mode, "--meter", "--log", log, *extra],
                       capture_output=True, text=True, env=env, cwd=cwd)
    entries = [json.loads(l) for l in open(log)]
    entry = [e for e in entries if e["status"] != "NO_PROPOSAL"][0]
    return p.stdout.strip().splitlines(), entry


def main():
    sys.stdout.reconfigure(line_buffering=True)
    env = dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(__file__), "..", "..", "src"))
    for title, program, _, code, extra in CASES:
        print(f"=== {title}")
        with tempfile.TemporaryDirectory() as d:
            for mode in ("plain", "live"):
                out, e = run(mode, program, code, extra, env, d)
                inplace = "n/a" if e["inplace_changes"] is None else e["inplace_changes"]
                print(f"  {mode:5}: {e['status']:24} residue={e['residue']}  "
                      f"external residue={e['external_residue']}  in-place changes={inplace}")
                for line in out:
                    print("         stdout:", line)
        print()


if __name__ == "__main__":
    main()