"""Demo of live healing (design D): five small programs, each run through live_run.

Run:  uv run python experiments/livehealer/demo.py
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap

CASES = [
    ("1. good repair, validated at the end of the function", """
        def f(d):
            v = d['x'] * 2
            print('inside f, v =', v)
            return v + 1
        print('result', f({'y': 1}))
     """, [{"match": "d['x']", "code": "v = 0"}], []),
    ("2. repair raises: rejected, original error reaches the program", """
        shared = [1, 2, 3]
        def f():
            v = 1 / 0
        try:
            f()
        except ZeroDivisionError:
            print('original error; shared =', shared)
     """, [{"match": "1 / 0", "code": "shared.append(99)\nraise ValueError('bad')"}], []),
    ("3. repair looks fine but the continuation runs a command: rejected", """
        import os
        def f():
            v = 1 / 0
            print('continuation output (must not appear)')
            os.system('echo side effect')
        try:
            f()
        except ZeroDivisionError:
            print('original error')
     """, [{"match": "1 / 0", "code": "v = 0"}], []),
    ("4. the original error happens again: rejected", """
        def f(d):
            v = d['x']
            w = d['x']
        try:
            f({})
        except KeyError:
            print('original error reached the program')
     """, [{"match": "v = d['x']", "code": "v = 0"}], []),
    ("5. two crashes: first validated at the next crash, second at the end", """
        def process(items):
            out = []
            for item in items:
                v = item['x'] * 2
                w = v + item['bonus']
                out.append(w)
            return out
        print(process([{'x': 1, 'bonus': 1}, {'y': 0, 'bonus': 5}, {'x': 2, 'bonus': None}]))
     """, [{"match": "item['x']", "code": "v = 0"}, {"match": "v + item", "code": "w = v"}], []),
]


def main():
    sys.stdout.reconfigure(line_buffering=True)
    env = dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(__file__), "..", "..", "src"))
    for title, program, rules, extra in CASES:
        print(f"=== {title}")
        with tempfile.TemporaryDirectory() as d:
            prog, rul, log = (os.path.join(d, n) for n in ("prog.py", "rules.json", "log.jsonl"))
            open(prog, "w").write(textwrap.dedent(program))
            json.dump(rules, open(rul, "w"))
            p = subprocess.run([sys.executable, "-m", "transactional_runtime_healing.live_run",
                                prog, rul, "--log", log, *extra],
                               capture_output=True, text=True, env=env, cwd=d)
            for line in p.stdout.splitlines():
                print("   stdout:", line)
            if p.stderr.strip():
                print("   stderr:", p.stderr.strip().splitlines()[-1])
            print("   exit status:", p.returncode)
            if os.path.exists(log):
                for line in open(log):
                    e = json.loads(line)
                    if e["status"] != "NO_PROPOSAL":
                        print(f"   heal line {e['line']}: {e['status']} - {e['reason']}")
        print()


if __name__ == "__main__":
    main()