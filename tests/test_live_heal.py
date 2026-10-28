"""Tests for live healing (design D). Each test runs a whole program in a subprocess,
because on commit the original process exits and the child carries on.

Run:  uv run pytest -v
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")
SRC = os.path.join(os.path.dirname(__file__), "..", "src")


# The original error must reach the program through a handler the instrumenter cannot see
# (a `try` in the same file would make the crash "handled by a caller", so it is not healed).
HOOK = "import sys\nsys.excepthook = lambda *a: print('original error')\n"


def run_live(tmp_path, program, rules, *args, hook=False, timeout=60):
    prog = tmp_path / "prog.py"
    prog.write_text((HOOK if hook else "") + textwrap.dedent(program))
    rul = tmp_path / "rules.json"
    rul.write_text(json.dumps(rules))
    log = tmp_path / "log.jsonl"
    env = dict(os.environ, PYTHONPATH=SRC)
    p = subprocess.run([sys.executable, "-m", "transactional_runtime_healing.live_run",
                        str(prog), str(rul), "--log", str(log), *args],
                       capture_output=True, text=True, env=env, timeout=timeout, cwd=tmp_path)
    entries = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    return p, entries


def statuses(entries):
    """Status of every heal attempt (crashes for which no repair was proposed are ignored)."""
    return [e["status"] for e in entries if e["status"] != "NO_PROPOSAL"]


# ---- commit --------------------------------------------------------------
def test_commit_at_end_of_function_and_output_appears_once(tmp_path):
    p, log = run_live(tmp_path, """
        def f(d):
            v = d['x'] * 2
            print('inside f, v =', v)
            return v + 1
        print('before')
        r = f({'y': 1})
        print('result', r)
    """, [{"match": "d['x']", "code": "v = 0"}])
    assert p.returncode == 0, p.stderr
    assert p.stdout.splitlines() == ["before", "inside f, v = 0", "result 1"]
    assert statuses(log) == ["COMMITTED"] and log[0]["validated_at"] == "end of function"


def test_two_crashes_in_one_function_validate_at_next_crash_then_function_end(tmp_path):
    p, log = run_live(tmp_path, """
        def process(items):
            out = []
            for item in items:
                v = item['x'] * 2
                w = v + item['bonus']
                out.append(w)
            return out
        print(process([{'x': 1, 'bonus': 1}, {'y': 0, 'bonus': 5}, {'x': 2, 'bonus': None}]))
    """, [{"match": "item['x']", "code": "v = 0"}, {"match": "v + item", "code": "w = v"}])
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == "[3, 5, 4]"
    assert [e["validated_at"] for e in log] == ["next crash", "end of function"]


def test_module_level_crash_validates_at_end_of_program(tmp_path):
    p, log = run_live(tmp_path, """
        x = 1 / 0
        print('x =', x)
    """, [{"match": "1 / 0", "code": "x = 'healed'"}])
    assert p.returncode == 0 and p.stdout.strip() == "x = healed"
    assert statuses(log) == ["COMMITTED"]


def test_exit_status_of_the_program_is_kept(tmp_path):
    p, _ = run_live(tmp_path, """
        import sys
        def f():
            v = 1 / 0
            return v
        f()
        sys.exit(3)
    """, [{"match": "1 / 0", "code": "v = 0"}])
    assert p.returncode == 3


def test_function_left_by_a_different_exception_still_validates(tmp_path):
    p, log = run_live(tmp_path, """
        def f(d):
            v = d['x']
            raise RuntimeError('intended later error')
        try:
            f({})
        except RuntimeError as e:
            print('caught', e)
    """, [{"match": "d['x']", "code": "v = 1"}])
    assert p.returncode == 0 and "caught intended later error" in p.stdout
    assert log[0]["validated_at"] == "function left by an exception"


def test_uncaught_exception_after_healing_is_the_programs_own(tmp_path):
    p, log = run_live(tmp_path, """
        def f(d):
            v = d['x']
            return int('abc')
        f({})
    """, [{"match": "d['x']", "code": "v = 1"}])
    assert p.returncode == 1 and "ValueError" in p.stderr
    assert statuses(log) == ["COMMITTED"]


# ---- reject: restore -----------------------------------------------------
def test_rejected_repair_leaves_state_and_output_untouched(tmp_path):
    p, log = run_live(tmp_path, """
        shared = [1, 2, 3]
        def f():
            v = 1 / 0
            return v
        print('start')
        f()
    """, [{"match": "1 / 0", "code": "shared.append(99)\nprint('trial output')\n"
                                    "raise ValueError('bad repair')"}])
    assert p.returncode == 1 and "ZeroDivisionError" in p.stderr
    assert p.stdout.splitlines() == ["start"]                  # nothing from the trial
    assert statuses(log) == ["REJECTED_REPAIR_ERROR"]


def test_parent_memory_is_untouched_after_reject(tmp_path):
    p, log = run_live(tmp_path, """
        import sys
        shared = [1, 2, 3]
        sys.excepthook = lambda *a: print('shared =', shared)
        def f():
            v = 1 / 0
            return v
        f()
    """, [{"match": "1 / 0", "code": "shared.append(99)\nraise ValueError('bad')"}])
    assert p.stdout.strip() == "shared = [1, 2, 3]"
    assert statuses(log) == ["REJECTED_REPAIR_ERROR"]


def test_effect_in_healing_code_is_rejected(tmp_path):
    p, log = run_live(tmp_path, """
        def f():
            v = 1 / 0
            return v
        f()
    """, [{"match": "1 / 0", "code": "import os\ntry:\n    os.system('true')\n"
                                    "except Exception:\n    pass\nv = 0"}], hook=True)
    assert p.stdout.strip() == "original error"
    assert statuses(log) == ["REJECTED_EXTERNAL_EFFECT"] and "os.system" in log[0]["effects"]


def test_effect_in_the_continuation_is_rejected_even_if_swallowed(tmp_path):
    marker = tmp_path / "marker"
    p, log = run_live(tmp_path, f"""
        import os
        def f():
            v = 1 / 0
            try:
                os.system('touch {marker}')
            except Exception:
                pass
            return v
        f()
    """, [{"match": "1 / 0", "code": "v = 0"}], hook=True)
    assert p.stdout.strip() == "original error"
    assert statuses(log) == ["REJECTED_EXTERNAL_EFFECT"]
    assert not marker.exists()


def test_allowed_event_is_not_blocked(tmp_path):
    p, log = run_live(tmp_path, """
        import os
        def f():
            v = 1 / 0
            os.system('true')
            return v
        f()
        print('done')
    """, [{"match": "1 / 0", "code": "v = 0"}], "--allow", "os.system")
    assert p.returncode == 0 and p.stdout.strip() == "done"
    assert statuses(log) == ["COMMITTED"]


def test_output_of_the_continuation_is_dropped_when_rejected(tmp_path):
    p, log = run_live(tmp_path, """
        import os
        def f():
            v = 1 / 0
            print('continuation output')
            os.system('true')
            return v
        f()
    """, [{"match": "1 / 0", "code": "v = 0"}], hook=True)
    assert p.stdout.splitlines() == ["original error"]
    assert statuses(log) == ["REJECTED_EXTERNAL_EFFECT"]


def test_strict_mode_rejects_when_the_same_error_comes_again(tmp_path):
    """Strict reading of proposal check 4 (not the default): in a loop it rejects repairs that
    plain healing would get right."""
    prog = """
        def f(items):
            for d in items:
                v = d['x']
        f([{}, {}])
    """
    p, log = run_live(tmp_path, prog, [{"match": "d['x']", "code": "v = 0"}],
                      "--recurrence", "reject", hook=True)
    assert p.stdout.strip() == "original error"
    assert statuses(log) == ["REJECTED_RECURRENCE"] and log[0]["category"] == "safety"


def test_same_error_type_at_another_place_is_not_a_recurrence(tmp_path):
    prog = """
        def f(d):
            v = d['x']
            w = d['x']
            return v
        f({})
        print('ok')
    """
    p, log = run_live(tmp_path, prog, [{"code": "v = 0\nw = 0"}])
    assert p.returncode == 0 and p.stdout.strip() == "ok"
    assert statuses(log) == ["COMMITTED", "COMMITTED"]


def test_same_error_again_is_just_the_next_crash_by_default(tmp_path):
    prog = """
        def f(items):
            out = []
            for d in items:
                v = d['x']
                out.append(v)
            return out
        print(f([{}, {}, {}]))
    """
    p, log = run_live(tmp_path, prog, [{"match": "d['x']", "code": "v = 0"}])
    assert p.returncode == 0 and p.stdout.strip() == "[0, 0, 0]"
    assert statuses(log) == ["COMMITTED"] * 3
    assert [e["repeat_count"] for e in log if e["status"] == "COMMITTED"] == [0, 1, 2]


# ---- exceptions the program handles itself ---------------------------------
def test_exception_handled_by_a_caller_is_not_healed(tmp_path):
    p, log = run_live(tmp_path, """
        def get(d):
            return d['x']
        def main():
            try:
                v = get({})
            except KeyError:
                v = 'default'
            print('v =', v)
        main()
    """, [{"match": "d['x']", "code": "_eh_rtn = 42"}])
    assert p.stdout.strip() == "v = default"
    assert [e["status"] for e in log] == ["SKIPPED"]
    assert "caller" in log[0]["reason"]


def test_handled_exception_during_a_trial_is_not_a_false_recurrence(tmp_path):
    p, log = run_live(tmp_path, """
        def get(d):
            return d['x']
        def f(d):
            v = d['x']                  # healed
            try:
                get({})                 # same exception type, but the program handles it
            except KeyError:
                pass
            return v
        print(f({}))
    """, [{"match": "v = d['x']", "code": "v = 5"}])
    assert p.returncode == 0 and p.stdout.strip() == "5"
    assert statuses(log) == ["SKIPPED", "COMMITTED"]      # the handled KeyError was skipped
    assert log[0]["category"] == "skipped" and log[1]["category"] == "committed"


def test_caller_that_catches_another_type_does_not_block_healing(tmp_path):
    p, log = run_live(tmp_path, """
        def get(d):
            return d['x']
        def main():
            try:
                v = get({})
            except ValueError:
                v = 'default'
            print('v =', v)
        main()
    """, [{"match": "d['x']", "code": "_eh_rtn = 42"}])
    assert p.stdout.strip() == "v = 42" and statuses(log) == ["COMMITTED"]


# ---- bookkeeping in the log ----------------------------------------------------
def test_log_records_the_exception_that_ended_the_trial(tmp_path):
    p, log = run_live(tmp_path, """
        def f(d):
            v = d['x']
            return int('abc')
        f({})
    """, [{"match": "d['x']", "code": "v = 1"}])
    assert log[0]["status"] == "COMMITTED" and "ValueError" in log[0]["continuation_error"]


def test_missing_function_defined_by_the_repair_is_kept(tmp_path):
    p, log = run_live(tmp_path, """
        def f(x):
            y = normalize(x)
            z = normalize(x + 1)
            return y + z
        print(f(1))
    """, [{"match": "y = normalize", "code": "def normalize(v):\n    return v * 10\ny = normalize(x)"}])
    assert p.returncode == 0 and p.stdout.strip() == "30"
    assert statuses(log) == ["COMMITTED"] and log[0]["leaked_globals"] == []


def test_helper_names_from_the_healing_code_do_not_leak_into_globals(tmp_path):
    p, log = run_live(tmp_path, """
        def f(d):
            v = d['x']
            return v
        f({})
        print('helper' in globals())
    """, [{"match": "d['x']", "code": "helper = 7\nv = helper"}])
    assert p.stdout.strip() == "False"
    assert log[0]["leaked_globals"] == ["helper"]


def test_timeout_is_an_operational_rejection(tmp_path):
    p, log = run_live(tmp_path, """
        def f():
            v = 1 / 0
            while True:
                pass
        f()
    """, [{"match": "1 / 0", "code": "v = 0"}], "--timeout", "1", hook=True)
    assert log[0]["status"] == "REJECTED_TIMEOUT" and log[0]["category"] == "operational"


def test_generator_validates_at_its_first_yield(tmp_path):
    p, log = run_live(tmp_path, """
        def gen(d):
            v = d['x']
            yield v
            yield v + 1
        for item in gen({}):
            print('item', item)
    """, [{"match": "d['x']", "code": "v = 10"}])
    assert p.stdout.splitlines() == ["item 10", "item 11"]
    assert log[0]["validated_at"] == "generator yielded"



def test_timeout_rejects_a_trial_that_never_reaches_the_validation_point(tmp_path):
    p, log = run_live(tmp_path, """
        def f():
            v = 1 / 0
            while True:
                pass
        f()
    """, [{"match": "1 / 0", "code": "v = 0"}], "--timeout", "1", hook=True)
    assert p.stdout.strip() == "original error"
    assert statuses(log) == ["REJECTED_TIMEOUT"]


def test_child_dying_before_the_validation_point_rejects(tmp_path):
    p, log = run_live(tmp_path, """
        import os
        def f():
            v = 1 / 0
            os._exit(7)
        f()
    """, [{"match": "1 / 0", "code": "v = 0"}], hook=True)
    assert p.stdout.strip() == "original error"
    assert statuses(log) == ["REJECTED_CHILD_DIED"]


def test_inherited_file_offset_is_restored_on_reject(tmp_path):
    data = tmp_path / "data.txt"
    data.write_text("0123456789")
    p, log = run_live(tmp_path, f"""
        import os
        f = open({str(data)!r})
        def g():
            v = 1 / 0
            f.read(5)
            os.system('true')
        import sys
        sys.excepthook = lambda *a: print(repr(f.read(3)))
        g()
    """, [{"match": "1 / 0", "code": "v = 0"}])
    assert p.stdout.strip() == "'012'"
    assert statuses(log) == ["REJECTED_EXTERNAL_EFFECT"]


def test_repair_not_tried_when_other_threads_exist(tmp_path):
    p, log = run_live(tmp_path, """
        import threading, time
        t = threading.Thread(target=time.sleep, args=(1,))
        t.start()
        def f():
            v = 1 / 0
        f()
        t.join()
    """, [{"match": "1 / 0", "code": "v = 0"}], hook=True)
    assert p.stdout.strip() == "original error"
    assert statuses(log) and set(statuses(log)) == {"SKIPPED"}
    assert "threads" in log[0]["reason"]


# ---- checks (library use) -------------------------------------------------
def test_failed_check_rejects(tmp_path):
    driver = tmp_path / "driver.py"
    driver.write_text(textwrap.dedent("""
        from transactional_runtime_healing.instrument import run_instrumented
        from transactional_runtime_healing.live_heal import LiveHealer
        src = '''
        def f():
            v = 1 / 0
            return v
        f()
        '''
        import sys
        sys.excepthook = lambda *a: print('original error')
        h = LiveHealer(lambda ctx: "v = 'text'" if "1 / 0" in ctx.statement else None,
                       checks=[lambda env: (isinstance(env.get('v'), int), 'v must be an int')],
                       log_path='check.jsonl')
        run_instrumented(src, h)
    """))
    p = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True,
                       env=dict(os.environ, PYTHONPATH=SRC), cwd=tmp_path, timeout=60)
    assert p.stdout.strip() == "original error"
    entry = json.loads((tmp_path / "check.jsonl").read_text().splitlines()[0])
    assert entry["status"] == "REJECTED_CHECK" and "v must be an int" in entry["reason"]