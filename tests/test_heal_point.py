"""Tests for heal_point (trial in a forked child, commit by replay).

Run:  uv run pytest -v
"""
import os
import signal
import time

import pytest

from transactional_runtime_healing.heal_point import HealStatus, heal_point


def heal(code, locals_, globals_=None, **kw):
    return heal_point(locals_, globals_ if globals_ is not None else {}, code, **kw)


# ---- the basic commit / restore behaviour ----
def test_good_heal_returns_bindings():
    loc = {"item": {"y": 1}, "v": None}
    r = heal("v = item.get('x', 0) * 2", loc)
    assert r.healed and r.bindings == {"v": 0}
    assert loc == {"item": {"y": 1}, "v": None}          # caller's dict is not modified


def test_rejected_heal_leaves_shared_objects_untouched():
    """HealBench leaves in-place changes behind after a failed attempt; we do not."""
    shared = [1, 2, 3]
    r = heal("data.append(99)\nraise ValueError('bad repair')", {"data": shared})
    assert r.status is HealStatus.REJECTED_HEAL_ERROR
    assert shared == [1, 2, 3]


def test_failed_check_leaves_shared_objects_untouched():
    shared = {"n": 1}
    r = heal("d['n'] = 'oops'", {"d": shared},
             checks=[lambda env: (isinstance(env["d"]["n"], int), "n must stay an int")])
    assert r.status is HealStatus.REJECTED_CHECK
    assert shared == {"n": 1}


def test_accepted_heal_applies_in_place_changes_to_shared_objects():
    shared = [1, 2, 3]
    r = heal("data.append(4)", {"data": shared})
    assert r.healed
    assert shared == [1, 2, 3, 4]                         # replay changed the live object
    assert r.bindings == {}                               # no binding changed


def test_global_binding_goes_to_globals_updates():
    r = heal("MODE = 'safe'", {"x": 1}, {"MODE": "fast"})
    assert r.healed and r.globals_updates == {"MODE": "safe"} and "MODE" not in r.bindings


def test_return_slot_eh_rtn_is_a_binding():
    r = heal("_eh_rtn = 42", {})
    assert r.healed and r.bindings["_eh_rtn"] == 42


def test_hang_times_out():
    r = heal("while True: pass", {}, timeout=1)
    assert r.status is HealStatus.REJECTED_TIMEOUT


# ---- effects: blocked in the trial, so nothing reaches the live process ----
@pytest.mark.parametrize("code", [
    "import subprocess; subprocess.run(['echo', 'x'])",
    "import subprocess\ntry:\n    subprocess.run(['echo', 'x'])\nexcept Exception:\n    pass",
    "import os; os.system('true')",
])
def test_external_effects_rejected(code):
    r = heal(code, {})
    assert r.status is HealStatus.REJECTED_EXTERNAL_EFFECT


def test_fs_write_before_rejected_effect_is_left_behind(tmp_path):
    marker = tmp_path / "marker"
    r = heal(f"import os\nopen({str(marker)!r}, 'w').write('x')\nos.system('true')", {})
    assert r.status is HealStatus.REJECTED_EXTERNAL_EFFECT
    assert marker.exists()                                # known limit: fs writes only recorded


# ---- replay risks ----
def test_replay_divergence_is_reported():
    r = heal("import os\nme = os.getpid()", {})
    assert r.healed and r.diverged and "me" in r.diverged_names


def test_deterministic_code_does_not_diverge():
    r = heal("total = sum(values)", {"values": [1, 2, 3]})
    assert r.healed and not r.diverged and r.bindings["total"] == 6


def test_replay_failure_is_reported():
    r = heal("import os\nif os.getpid() == %d:\n    raise RuntimeError('only in live process')"
             % os.getpid(), {})
    assert r.status is HealStatus.REPLAY_FAILED


def test_effect_only_in_replay_and_swallowed_is_blocked():
    code = ("import os, subprocess\n"
            "try:\n"
            "    if os.getpid() == LIVE:\n"
            "        subprocess.run(['echo', 'x'])\n"
            "except Exception:\n"
            "    pass")
    r = heal(code, {}, {"LIVE": os.getpid()})
    assert r.status is HealStatus.REPLAY_BLOCKED
    assert "subprocess.Popen" in r.effects


def test_checks_run_again_on_the_replayed_result():
    data = []
    code = "import os\ndata.append('ok' if os.getpid() != LIVE else None)"
    r = heal(code, {"data": data}, {"LIVE": os.getpid()},
             checks=[lambda env: (None not in env["data"], "no None allowed")])
    assert r.status is HealStatus.REPLAY_CHECK_FAILED
    assert r.reason == "no None allowed"
    assert r.diverged and "data" in r.diverged_names     # in-place divergence is seen
    assert data == [None]                                # reported, not hidden


def test_in_place_divergence_is_reported_even_when_checks_pass():
    data = []
    code = "import os\ndata.append(os.getpid())"
    r = heal(code, {"data": data})
    assert r.healed and r.diverged and r.diverged_names == ["data"]


def test_modules_are_not_returned_as_bindings():
    r = heal("import os\nx = 1", {})
    assert r.healed and r.bindings == {"x": 1}


def test_programs_own_alarm_handler_and_timer_survive():
    seen = []
    old = signal.signal(signal.SIGALRM, lambda *a: seen.append(1))
    try:
        signal.setitimer(signal.ITIMER_REAL, 30)
        r = heal("y = 1", {})
        delay, _ = signal.getitimer(signal.ITIMER_REAL)
        handler = signal.getsignal(signal.SIGALRM)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
    assert r.healed
    assert 0 < delay <= 30                       # program's timer was re-armed
    assert callable(handler) and handler is not signal.SIG_DFL


def test_replay_timeout_cannot_be_swallowed_by_except_exception():
    code = ("import os\n"
            "if os.getpid() == LIVE:\n"
            "    while True:\n"
            "        try:\n"
            "            pass\n"
            "        except Exception:\n"
            "            pass")
    t0 = time.monotonic()
    r = heal(code, {}, {"LIVE": os.getpid()}, replay_timeout=1)
    assert r.status is HealStatus.REPLAY_FAILED
    assert time.monotonic() - t0 < 5


def test_output_of_rejected_trial_is_held_back_at_fd_level(capfd):
    heal("import os\nos.write(1, b'LEAKED\\n')\nraise ValueError('bad')", {})
    assert "LEAKED" not in capfd.readouterr().out


def test_fd_offset_change_is_listed_separately():
    import tempfile
    with tempfile.TemporaryFile("w+") as f:
        f.write("0123456789")
        f.flush()
        f.seek(0)
        r = heal("f.read(5)", {"f": f})
        assert r.status is HealStatus.REJECTED_INHERITED_FD
        assert r.fd_effects and r.effects == []
        assert f.tell() == 0


# ---- a program that crashes twice ----
def process(items):
    """Toy program: two different crashes, each healed at its own heal point."""
    results = []
    for item in items:
        try:
            v = item["x"] * 2                              # crash 1: KeyError
        except KeyError:
            r = heal_point(dict(locals()), globals(), "v = 0", timeout=5)
            if not r.healed:
                raise
            v = r.bindings["v"]
        try:
            w = v + item["bonus"]                          # crash 2: TypeError (None)
        except TypeError:
            r = heal_point(dict(locals()), globals(), "w = v", timeout=5)
            if not r.healed:
                raise
            w = r.bindings["w"]
        results.append(w)
    return results


def test_two_heal_points_in_one_run():
    items = [{"x": 1, "bonus": 1}, {"y": 0, "bonus": 5}, {"x": 2, "bonus": None}]
    assert process(items) == [3, 5, 4]