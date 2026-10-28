"""Tests for the instrumenter: real exceptions are caught and healed (or not).

Run:  uv run pytest -v
"""
import traceback

import pytest

from transactional_runtime_healing.instrument import (
    Healer, instrumented_source, run_instrumented)


def run(src, propose, **kw):
    healer = Healer(propose, **kw)
    g = run_instrumented(src, healer)
    return g, healer


def by_statement(mapping):
    """propose() that returns healing code chosen by the failing statement's text."""
    def propose(ctx):
        for key, code in mapping.items():
            if key in ctx.statement:
                return code
        return None
    return propose


# ---- basic healing ---------------------------------------------------
def test_assignment_crash_is_healed_and_execution_continues():
    src = "def f(d):\n    v = d['x'] * 2\n    return v + 1\nresult = f({'y': 1})\n"
    g, h = run(src, by_statement({"d['x']": "v = 0"}))
    assert g["result"] == 1                      # v was set by the healing code
    assert len(h.log) == 1 and h.log[0].result.healed


def test_return_expression_crash_uses_eh_rtn():
    src = "def f(data):\n    return int(data)\nresult = f('abc')\n"
    g, _ = run(src, by_statement({"int(data)": "_eh_rtn = -1"}))
    assert g["result"] == -1


def test_return_without_eh_rtn_returns_none():
    src = "def f(data):\n    return int(data)\nresult = f('abc')\n"
    g, _ = run(src, by_statement({"int(data)": "pass"}))
    assert g["result"] is None


def test_failed_statement_is_not_retried():
    src = ("calls = []\n"
           "def f():\n"
           "    calls.append(1)\n"
           "    x = 1 / 0\n"
           "    calls.append(2)\n"
           "f()\n")
    g, _ = run(src, by_statement({"1 / 0": "pass"}))
    assert g["calls"] == [1, 2]


def test_two_crashes_in_one_run():
    src = ("def process(items):\n"
           "    out = []\n"
           "    for item in items:\n"
           "        v = item['x'] * 2\n"
           "        w = v + item['bonus']\n"
           "        out.append(w)\n"
           "    return out\n"
           "result = process([{'x': 1, 'bonus': 1}, {'y': 0, 'bonus': 5}, {'x': 2, 'bonus': None}])\n")
    g, h = run(src, by_statement({"item['x']": "v = 0", "v + item": "w = v"}))
    assert g["result"] == [3, 5, 4]
    assert len(h.log) == 2


def test_module_level_crash_healed_and_globals_updated():
    src = "x = 1 / 0\ny = 5\n"
    g, _ = run(src, by_statement({"1 / 0": "x = 'healed'"}))
    assert g["x"] == "healed" and g["y"] == 5


def test_load_only_name_goes_to_globals():
    src = "def f():\n    return missing_name\nresult = f()\n"
    g, _ = run(src, by_statement({"missing_name": "_eh_rtn = 3"}))
    assert g["result"] == 3


def test_for_header_crash_skips_the_loop():
    """HealBench case: a for header that raises; healing returns, the loop is skipped."""
    src = ("def f(headers):\n"
           "    total = 0\n"
           "    for name, value in headers:\n"
           "        total += 1\n"
           "    total += 100\n"
           "    return total\n"
           "result = f({'a': 1})\n")
    g, _ = run(src, by_statement({"for name, value": "pass"}))
    assert g["result"] == 100


def test_methods_and_generators_are_instrumented():
    src = ("class A:\n"
           "    def m(self, d):\n"
           "        v = d['k']\n"
           "        return v\n"
           "def gen():\n"
           "    yield 1\n"
           "    x = 1 / 0\n"
           "    yield 2\n"
           "r = A().m({})\n"
           "vals = list(gen())\n")
    g, _ = run(src, by_statement({"d['k']": "v = 'ok'", "1 / 0": "pass"}))
    assert g["r"] == "ok" and g["vals"] == [1, 2]


# ---- transactional behaviour through the instrumenter ----------------
def test_rejected_healing_propagates_the_original_exception_and_leaves_objects_untouched():
    src = ("shared = [1, 2, 3]\n"
           "def f():\n"
           "    v = 1 / 0\n"
           "f()\n")
    healer = Healer(by_statement({"1 / 0": "shared.append(99)\nraise ValueError('bad repair')"}))
    g = {}
    with pytest.raises(ZeroDivisionError):
        run_instrumented(src, healer, globals_=g)
    assert g["shared"] == [1, 2, 3]              # HealBench would leave 99 behind
    assert healer.log[0].result.status.name == "REJECTED_HEAL_ERROR"


def test_accepted_healing_changes_shared_objects_in_place():
    src = ("shared = [1]\n"
           "def f():\n"
           "    v = 1 / 0\n"
           "f()\n")
    g, _ = run(src, by_statement({"1 / 0": "shared.append(2)"}))
    assert g["shared"] == [1, 2]


def test_external_effect_in_healing_code_is_blocked():
    src = "def f():\n    v = 1 / 0\nf()\n"
    healer = Healer(by_statement({"1 / 0": "import os\nos.system('true')"}))
    with pytest.raises(ZeroDivisionError):
        run_instrumented(src, healer)
    assert healer.log[0].result.status.name == "REJECTED_EXTERNAL_EFFECT"


def test_check_failure_blocks_the_commit():
    src = "def f():\n    v = 1 / 0\n    return v\nf()\n"
    healer = Healer(by_statement({"1 / 0": "v = 'text'"}),
                    checks=[lambda env: (isinstance(env.get("v"), int), "v must be an int")])
    with pytest.raises(ZeroDivisionError):
        run_instrumented(src, healer)
    assert healer.log[0].result.reason == "v must be an int"


# ---- what is not instrumented ----------------------------------------
def test_expected_exceptions_in_try_body_are_not_healed():
    src = ("def f(s):\n"
           "    try:\n"
           "        n = int(s)\n"
           "    except ValueError:\n"
           "        n = -1\n"
           "    return n\n"
           "result = f('abc')\n")
    calls = []
    g, h = run(src, lambda ctx: calls.append(ctx) or "n = 99")
    assert g["result"] == -1 and calls == []     # the program's own handler ran


def test_other_exceptions_in_that_try_body_can_still_be_healed():
    src = ("def f(d):\n"
           "    try:\n"
           "        n = d['k']\n"
           "    except ValueError:\n"
           "        n = -1\n"
           "    return n\n"
           "result = f({})\n")
    g, h = run(src, by_statement({"d['k']": "n = 7"}))
    assert g["result"] == 7 and len(h.log) == 1


def test_catch_all_handler_disables_wrapping_inside_its_try_body():
    src = ("def f():\n"
           "    try:\n"
           "        x = 1 / 0\n"
           "    except Exception:\n"
           "        x = 'program handled it'\n"
           "    return x\n"
           "result = f()\n")
    calls = []
    g, _ = run(src, lambda ctx: calls.append(1) or "x = 0")
    assert g["result"] == "program handled it" and calls == []


def test_raise_is_not_wrapped_but_the_caller_still_sees_the_exception():
    """The raise line is never a heal point; the exception travels up to the caller's
    statement, which is a heal point (as in HealBench the healer may re-raise)."""
    src = ("def f():\n"
           "    raise RuntimeError('intended')\n"
           "f()\n")
    linenos = []
    with pytest.raises(RuntimeError):
        run(src, lambda ctx: linenos.append(ctx.lineno))     # returns None: do not heal
    assert linenos == [3]                                    # the call site, not line 2


def test_healer_returning_none_lets_the_exception_through():
    src = "def f():\n    v = 1 / 0\nf()\n"
    with pytest.raises(ZeroDivisionError):
        run(src, lambda ctx: None)


def test_budget_limits_the_number_of_heals():
    src = ("def f():\n"
           "    for i in range(5):\n"
           "        v = 1 / 0\n"
           "f()\n")
    healer = Healer(by_statement({"1 / 0": "pass"}), max_heals=2)
    with pytest.raises(ZeroDivisionError):
        run_instrumented(src, healer)
    assert len(healer.log) == 2


# ---- bookkeeping ------------------------------------------------------
def test_context_contains_crash_information():
    src = "def f(d):\n    v = d['x']\nf({'a': 1})\n"
    seen = []
    run(src, lambda ctx: seen.append(ctx) or "v = 0")
    ctx = seen[0]
    assert isinstance(ctx.exception, KeyError)
    assert ctx.lineno == 2 and ctx.function == "f" and "d['x']" in ctx.statement
    assert ctx.locals_["d"] == {"a": 1}


def test_line_numbers_in_tracebacks_are_preserved():
    src = "def f():\n    a = 1\n    b = 1 / 0\nf()\n"
    with pytest.raises(ZeroDivisionError) as info:
        run(src, lambda ctx: None)
    lines = [fs.lineno for fs in traceback.extract_tb(info.value.__traceback__)
             if fs.filename == "<instrumented>"]
    assert 3 in lines


def test_docstring_and_future_import_survive():
    src = ('from __future__ import annotations\n'
           'def f():\n'
           '    """doc"""\n'
           '    return 1\n'
           'result = f()\n'
           'doc = f.__doc__\n')
    g, _ = run(src, lambda ctx: None)
    assert g["result"] == 1 and g["doc"] == "doc"


def test_instrumented_source_shows_the_rewrite():
    text = instrumented_source("def f(d):\n    return d['x']\n")
    assert "_eh_rtn" in text and "__heal__" in text and "except Exception as __eh_e" in text


# ---- exceptions the program handles itself / helper names -------------------
def test_exception_handled_by_a_caller_is_not_healed():
    src = ("def get(d):\n"
           "    return d['x']\n"
           "def main():\n"
           "    try:\n"
           "        v = get({})\n"
           "    except KeyError:\n"
           "        v = 'default'\n"
           "    return v\n"
           "result = main()\n")
    g, h = run(src, by_statement({"d['x']": "_eh_rtn = 42"}))
    assert g["result"] == "default" and h.log == [] and h.skipped_caller_handled == 1


def test_caller_with_tuple_handler_and_catch_all_are_seen():
    for handler in ("(KeyError, ValueError)", "Exception", "LookupError"):
        src = (f"def get(d):\n    return d['x']\ndef main():\n    try:\n        v = get({{}})\n"
               f"    except {handler}:\n        v = 'default'\n    return v\nresult = main()\n")
        g, h = run(src, by_statement({"d['x']": "_eh_rtn = 42"}))
        assert g["result"] == "default", handler


def test_caller_with_a_handler_for_another_type_still_allows_healing():
    src = ("def get(d):\n    return d['x']\ndef main():\n    try:\n        v = get({})\n"
           "    except ValueError:\n        v = 'default'\n    return v\nresult = main()\n")
    g, _ = run(src, by_statement({"d['x']": "_eh_rtn = 42"}))
    assert g["result"] == 42


def test_exception_rejected_once_is_not_offered_to_enclosing_statements():
    src = ("def f(items):\n    for d in items:\n        v = d['x']\n"
           "f([{}])\n")
    calls = []
    healer = Healer(lambda ctx: calls.append(ctx.statement) or "raise ValueError('bad')")
    with pytest.raises(KeyError):
        run_instrumented(src, healer)
    assert len(calls) == 1


def test_helper_names_in_healing_code_are_not_added_to_globals():
    src = "def f(d):\n    v = d['x']\n    return v\nresult = f({})\n"
    g, h = run(src, by_statement({"d['x']": "helper = 7\nv = helper"}))
    assert g["result"] == 7 and "helper" not in g
    assert h.log[0].result.leaked_globals == ["helper"]


def test_new_name_that_the_program_reads_is_added_to_globals():
    src = ("def f(x):\n    y = normalize(x)\n    z = normalize(x + 1)\n    return y + z\n"
           "result = f(1)\n")
    g, h = run(src, by_statement({"y = normalize": "def normalize(v):\n    return v * 10\ny = normalize(x)"}))
    assert g["result"] == 30 and len(h.log) == 1
    assert h.log[0].result.leaked_globals == []


def test_handler_expression_with_a_call_is_not_evaluated():
    src = ("def get(d):\n    return d['x']\ndef main():\n    try:\n        v = get({})\n"
           "    except make_handler():\n        v = 'default'\n    return v\n"
           "def make_handler():\n    raise SystemError('must not be called')\n"
           "result = main()\n")
    g, h = run(src, by_statement({"d['x']": "_eh_rtn = 1"}))
    assert g["result"] == 1