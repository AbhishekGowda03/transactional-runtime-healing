"""Run a Python program with live healing.

    python -m transactional_runtime_healing.live_run PROGRAM.py RULES.json [options]
    python -m transactional_runtime_healing.live_run PROGRAM.py --llm [options]

RULES.json is a list of rules, tried in order for each crash:
    [{"match": "d['x']", "exception": "KeyError", "code": "v = 0"}, ...]
`match` (substring of the failing statement) and `exception` (type name) are optional;
`code` is the healing code. In the real system an LLM writes this code; rules make the
protocol testable without one.

With --llm the healing code comes from a language model instead of RULES.json (see llm.py:
--model, --api-base, --cache-dir, --replay-only, --dry-run, --max-calls; the API key is read
from the environment variable TXH_API_KEY, never from the command line).

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
    ap.add_argument("rules", nargs="?", help="rules file (not needed with --llm)")
    ap.add_argument("--llm", action="store_true", help="ask a language model for the repairs")
    ap.add_argument("--model")
    ap.add_argument("--api-base")
    ap.add_argument("--cache-dir")
    ap.add_argument("--max-calls", type=int)
    ap.add_argument("--temperature", type=float)
    ap.add_argument("--run-id", help="repeat number: asked again instead of replayed")
    ap.add_argument("--prompt", choices=("rules", "minimal"))
    ap.add_argument("--llm-events", help="append one JSON line per model request (cache or "
                                         "model, ok / decline / invalid / error ...) to this file")
    ap.add_argument("--replay-only", action="store_true",
                    help="use cached answers only; never call the model")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the prompts, call nothing, heal nothing")
    ap.add_argument("--log")
    ap.add_argument("--mode", choices=("live", "plain"), default="live")
    ap.add_argument("--meter", action="store_true")
    ap.add_argument("--guard", choices=("heal-phase", "taint"), nargs="?", const="taint",
                    help="plain mode: heal-phase = stop protected operations of the healing "
                         "code only; taint = also follow values it introduced (HealGuard-style)")
    ap.add_argument("--post-commit", choices=("off", "warn", "block"), default="warn",
                    help="live mode: guard behaviour after the validation point")
    ap.add_argument("--require-targets", action="store_true",
                    help="live mode: reject a repair that leaves unbound the names the failing "
                         "statement would have bound (or never sets _eh_rtn for a return)")
    ap.add_argument("--no-guard", action="store_true",
                    help="live mode: switch the protected-operations check off")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--recurrence", choices=("commit", "reject"), default="commit")
    ap.add_argument("--allow", action="append", default=[])
    ap.add_argument("--show-instrumented", action="store_true")
    ns = ap.parse_args(argv)

    with open(ns.program) as f:
        source = f.read()
    if ns.llm:
        from transactional_runtime_healing.llm import LLMPropose
        propose = LLMPropose(model=ns.model, api_base=ns.api_base, cache_dir=ns.cache_dir,
                             max_calls=ns.max_calls, replay_only=ns.replay_only,
                             dry_run=ns.dry_run, temperature=ns.temperature,
                             run_id=ns.run_id, prompt=ns.prompt, events_path=ns.llm_events)
    else:
        if not ns.rules:
            ap.error("a rules file is required unless --llm is given")
        with open(ns.rules) as f:
            propose = rules_propose(json.load(f))
    if ns.show_instrumented:
        print(instrumented_source(source, ns.program))
        return 0
    if ns.mode == "plain":
        healer = PlainHealer(propose, timeout=ns.timeout, log_path=ns.log,
                             meter_state=ns.meter, guard=ns.guard or False)
    else:
        healer = LiveHealer(propose, timeout=ns.timeout, recurrence=ns.recurrence,
                            allow_events=ns.allow, log_path=ns.log, meter_state=ns.meter,
                            guard=not ns.no_guard, post_commit=ns.post_commit,
                            require_targets=ns.require_targets)
    sys.argv = [ns.program]
    run_instrumented(source, healer, filename=ns.program,
                     globals_={"__name__": "__main__", "__file__": ns.program})
    return 0


if __name__ == "__main__":
    sys.exit(main())