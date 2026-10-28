"""Demo: real crashes are caught, healed in a trial child, and committed by replay.

The program below crashes twice. The 'LLM' is replaced by hard-coded healing code.
"""
from transactional_runtime_healing.instrument import (
    Healer, instrumented_source, run_instrumented)

PROGRAM = '''
shared_log = []

def process(items):
    out = []
    for item in items:
        v = item["x"] * 2              # crash 1: KeyError for {"y": ...}
        w = v + item["bonus"]          # crash 2: TypeError when bonus is None
        out.append(w)
    return out

result = process([{"x": 1, "bonus": 1}, {"y": 0, "bonus": 5}, {"x": 2, "bonus": None}])
'''

CODE = {
    'v = item["x"] * 2': "v = 0",
    "w = v + item": "w = v",
}


def propose(ctx):
    for key, code in CODE.items():
        if key in ctx.statement:
            return code
    return None


def show(title):
    print(f"\n=== {title} ===")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(line_buffering=True)

    show("1. What the instrumenter does to one function")
    print(instrumented_source("def f(d):\n    v = d['x']\n    return v\n"))

    show("2. A program that crashes twice, healed")
    healer = Healer(propose)
    g = run_instrumented(PROGRAM, healer)
    print("result:", g["result"], "(expected [3, 5, 4])")
    for rec in healer.log:
        r = rec.result
        print(f"  line {rec.context.lineno}: {type(rec.context.exception).__name__} -> "
              f"{r.status.name}, changed={r.changed}, diverged={r.diverged}")

    show("3. A bad healing code is rejected, shared objects stay untouched")
    prog = "shared = [1, 2, 3]\ndef f():\n    v = 1 / 0\nf()\n"
    bad = Healer(lambda ctx: "shared.append(99)\nraise ValueError('bad repair')")
    g = {}
    try:
        run_instrumented(prog, bad, globals_=g)
    except ZeroDivisionError as e:
        print("original exception propagated:", repr(e))
    print("shared after the rejected attempt:", g["shared"], "(plain healing would leave 99)")
    print("status:", bad.log[0].result.status.name)

    show("4. Healing code that tries an external effect")
    prog = "def f():\n    v = 1 / 0\nf()\n"
    ext = Healer(lambda ctx: "import os\nos.system('echo should not run')")
    try:
        run_instrumented(prog, ext)
    except ZeroDivisionError as e:
        print("original exception propagated:", repr(e))
    print("status:", ext.log[0].result.status.name, ext.log[0].result.effects)