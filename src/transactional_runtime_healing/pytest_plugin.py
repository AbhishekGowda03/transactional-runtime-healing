"""pytest plugin: run the tests of a repository with runtime healing switched on.

    PYTHONPATH=<repo>:<src> TXH_HEAL_ROOT=<repo> TXH_HEAL_MODE=live \
        python -m pytest -p transactional_runtime_healing.pytest_plugin -s <tests>

It must be loaded with `-p` so that it is active before the repository is imported. Settings
(environment variables, because the plugin starts before pytest reads its options):

    TXH_HEAL_ROOT       repository folder to instrument (required)
    TXH_HEAL_MODE       plain | live                       default live
    TXH_HEAL_RULES      JSON rules file instead of a language model (for tests)
    TXH_HEAL_LOG        JSON-lines log of heal attempts
    TXH_HEAL_CACHE      folder for the instrumented-code cache
    TXH_HEAL_GUARD      taint | heal-phase | off           plain mode guard   default off
    TXH_HEAL_POST_COMMIT off | warn | block                live mode          default warn
    TXH_HEAL_NO_GUARD   1                                  live mode: protected-operations
                                                           check off (the "fork only" setting)
    TXH_HEAL_REQUIRE_TARGETS 1                             live mode: completeness check
    TXH_HEAL_ONLY       `file:line;file:line` crash sites: only exceptions raised there are
                        repaired (the crash points of the failing run); default: any site
    TXH_HEAL_ALL        1 = also heal outside test bodies (collection, fixtures); default: the
                        model is only asked while a test function is running
    TXH_HEAL_STATS      file that receives the model plug-in's counters (JSON) at the end
    TXH_LLM_EVENTS      file for one line per model request
    TXH_*               the model settings of llm.py (model, API base, cache dir, run id, ...)

Run with -s (no output capture): the live protocol holds back the program's output itself.
"""
import json
import os
import sys

from transactional_runtime_healing import repo_hook

ROOT = os.environ.get("TXH_HEAL_ROOT")
FINDER = None
PROPOSE = None
ACTIVE = [False]              # True while a test function body is running


def _gated(propose):
    """Only ask for repairs while a test body runs: not during imports, collection or fixtures,
    where libraries (and pytest itself) raise exceptions on purpose all the time."""
    if os.environ.get("TXH_HEAL_ALL"):
        return propose
    only = []
    for part in os.environ.get("TXH_HEAL_ONLY", "").split(";"):
        if ":" in part:
            f, _, n = part.rpartition(":")
            if n.strip().isdigit():
                only.append((f.strip(), int(n)))

    def at_site(ctx):
        if not only:
            return True
        last = ctx.lineno + ctx.statement.count("\n")
        return any(ctx.filename.endswith(f) and ctx.lineno <= n <= last for f, n in only)

    def gated(ctx):
        return propose(ctx) if ACTIVE[0] and at_site(ctx) else None
    return gated


def build_healer(env=os.environ):
    mode = env.get("TXH_HEAL_MODE", "live")
    log = env.get("TXH_HEAL_LOG")
    if env.get("TXH_HEAL_RULES"):
        from transactional_runtime_healing.live_run import rules_propose
        with open(env["TXH_HEAL_RULES"]) as f:
            propose = rules_propose(json.load(f))
    else:
        from transactional_runtime_healing.llm import LLMPropose
        propose = LLMPropose(events_path=env.get("TXH_LLM_EVENTS"))
        global PROPOSE
        PROPOSE = propose
    propose = _gated(propose)
    if mode == "plain":
        from transactional_runtime_healing.plain_heal import PlainHealer
        guard = env.get("TXH_HEAL_GUARD", "off")
        return PlainHealer(propose, log_path=log, guard=False if guard == "off" else guard)
    from transactional_runtime_healing.live_heal import LiveHealer
    return LiveHealer(propose, log_path=log, guard=not env.get("TXH_HEAL_NO_GUARD"),
                      post_commit=env.get("TXH_HEAL_POST_COMMIT", "warn"),
                      require_targets=bool(env.get("TXH_HEAL_REQUIRE_TARGETS")))


if ROOT:
    from transactional_runtime_healing import instrument as _instrument
    _instrument.EXPECT_FROM_TESTS = True          # `with raises(...)` in a test means: expected
    FINDER = repo_hook.install(ROOT, build_healer(), os.environ.get("TXH_HEAL_CACHE"))


import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    ACTIVE[0] = True
    try:
        yield
    finally:
        ACTIVE[0] = False


def pytest_report_header(config):
    if FINDER is None:
        return "runtime healing: OFF (TXH_HEAL_ROOT is not set)"
    return (f"runtime healing: {os.environ.get('TXH_HEAL_MODE', 'live')} mode, "
            f"root {ROOT}")


def pytest_terminal_summary(terminalreporter):
    if FINDER is not None:
        terminalreporter.write_line(f"runtime healing: {len(FINDER.instrumented)} modules "
                                    f"instrumented, {len(FINDER.skipped)} left unchanged")
    path = os.environ.get("TXH_HEAL_STATS")
    if path and PROPOSE is not None:
        with open(path, "w") as f:
            json.dump(PROPOSE.stats, f)