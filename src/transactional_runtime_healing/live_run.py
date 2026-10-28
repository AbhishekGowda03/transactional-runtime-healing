"""Run a Python program with live healing.

    python -m transactional_runtime_healing.live_run PROGRAM.py RULES.json [options]

RULES.json is a list of rules, tried in order for each crash:
    [{"match": "d['x']", "exception": "KeyError", "code": "v = 0"}, ...]
`match` (substring of the failing statement) and `exception` (type name) are optional;
`code` is the healing code. In the real system an LLM writes this code; rules make the
protocol testable without one.

Options: --mode live|plain (live = the protocol, plain = HealBench-style baseline),
         --meter (measure residual state), --log FILE (JSON lines), --timeout SECONDS, --recurrence commit|reject,
         --allow EVENT (repeatable), --show-instrumented
The exit status is the program's own (uncaught exception: 1, as usual).
"""
import argparse
import json
import sys

from transactional_runtime_healing.instrument import instrumented_source, run_instrumented
from transactional_runtime_healing.live_heal import LiveHealer
from transactional_runtime_healing.plain_heal import PlainHealer


def rules_propose(rules):
    def propose(ctx):
        for rule in rules:
            if rule.get("match") and rule["match"] not in ctx.statement:
                continue
            if rule.get("exception") and rule["exception"] != type(ctx.exception).__name__:
                continue
            return rule["code"]
        return None
    return propose


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("program")
    ap.add_argument("rules")
    ap.add_argument("--log")
    ap.add_argument("--mode", choices=("live", "plain"), default="live")
    ap.add_argument("--meter", action="store_true")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--recurrence", choices=("commit", "reject"), default="commit")
    ap.add_argument("--allow", action="append", default=[])
    ap.add_argument("--show-instrumented", action="store_true")
    ns = ap.parse_args(argv)

    with open(ns.program) as f:
        source = f.read()
    with open(ns.rules) as f:
        rules = json.load(f)
    if ns.show_instrumented:
        print(instrumented_source(source, ns.program))
        return 0
    if ns.mode == "plain":
        healer = PlainHealer(rules_propose(rules), timeout=ns.timeout, log_path=ns.log,
                             meter_state=ns.meter)
    else:
        healer = LiveHealer(rules_propose(rules), timeout=ns.timeout, recurrence=ns.recurrence,
                            allow_events=ns.allow, log_path=ns.log, meter_state=ns.meter)
    sys.argv = [ns.program]
    run_instrumented(source, healer, filename=ns.program,
                     globals_={"__name__": "__main__", "__file__": ns.program})
    return 0


if __name__ == "__main__":
    sys.exit(main())