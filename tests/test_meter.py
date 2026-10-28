"""Tests for the residual-state meter."""
import os
import sys

from transactional_runtime_healing.meter import diff, snapshot


def snap(g):
    return snapshot(None, g)


def test_unchanged_state_has_no_difference():
    g = {"a": [1, 2], "b": {"k": 1}}
    assert diff(snap(g), snap(g)) == []


def test_in_place_change_is_seen():
    g = {"a": [1, 2]}
    before = snap(g)
    g["a"].append(3)
    assert diff(before, snap(g)) == ["global:a"]


def test_rebinding_new_and_deleted_names_are_seen():
    g = {"a": 1, "b": 2}
    before = snap(g)
    g["a"] = 5
    g["c"] = 1
    del g["b"]
    assert diff(before, snap(g)) == ["global:a", "global:b", "global:c"]


def test_healer_names_are_ignored_but_functions_and_modules_are_not():
    g = {"__heal__": object(), "__eh_r": 1, "f": lambda: 1}
    assert [k for k in snap(g) if k.startswith("global:")] == ["global:f"]


def test_rebinding_a_global_function_to_another_callable_is_seen():
    g = {"parse": int}
    before = snap(g)
    g["parse"] = float
    assert diff(before, snap(g)) == ["global:parse"]


def test_class_attribute_monkeypatch_is_seen():
    class C:
        def m(self):
            return 1
    g = {"C": C}
    before = snap(g)
    C.m = lambda self: 2
    assert diff(before, snap(g)) == ["global:C"]


def test_flag_in_a_library_module_is_seen(monkeypatch):
    import sys
    import types
    lib = types.ModuleType("fakelib_for_meter")
    lib.LOAD_TRUNCATED = False
    lib.parse = int
    monkeypatch.setitem(sys.modules, "fakelib_for_meter", lib)
    before = snap({})
    lib.LOAD_TRUNCATED = True
    lib.parse = float
    assert diff(before, snap({})) == ["module:fakelib_for_meter.LOAD_TRUNCATED",
                                      "module:fakelib_for_meter.parse"]


def test_monkeypatch_of_a_standard_library_function_is_seen(monkeypatch):
    import json
    before = snap({})
    monkeypatch.setattr(json, "loads", float)
    assert diff(before, snap({})) == ["module:json.loads"]


def test_object_holding_a_lock_is_fingerprinted_by_its_attributes():
    import threading

    class Session:
        def __init__(self):
            self.lock = threading.Lock()
            self.retries = 3
    sess = Session()
    g = {"sess": sess}
    before = snap(g)
    sess.retries = 0
    assert diff(before, snap(g)) == ["global:sess"]


def test_cyclic_structures_do_not_loop():
    class Node:
        pass
    a, b = Node(), Node()
    a.other, b.other = b, a
    a.lock = __import__("threading").Lock()
    g = {"a": a}
    assert diff(snap(g), snap(g)) == []


def test_sys_path_change_is_seen(monkeypatch):
    import sys
    before = snap({})
    monkeypatch.setattr(sys, "path", sys.path + ["/nonexistent-meter-test"])
    assert "@sys.path" in diff(before, snap({}))


def test_environment_and_cwd_changes_are_seen(tmp_path, monkeypatch):
    g = {}
    before = snap(g)
    monkeypatch.setenv("METER_TEST_VAR", "1")
    assert diff(before, snap(g)) == ["@environ"]
    before = snap(g)
    monkeypatch.chdir(tmp_path)
    assert "@cwd" in diff(before, snap(g))


def test_file_offset_change_is_seen(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("0123456789")
    with open(f) as fh:
        before = snap({})
        fh.read(4)
        assert any(k.startswith("@fd:") for k in diff(before, snap({})))


def test_unpicklable_objects_are_fingerprinted():
    g = {"gen": (i for i in range(3))}
    assert diff(snap(g), snap(g)) == []


def test_first_snapshot_is_stable_although_fingerprinting_touches_lazy_module_attributes():
    """Pickling a class looks it up by name; `ast` creates deprecated aliases on first access.
    The first snapshot of a process must not differ from the second for that reason."""
    import subprocess
    import sys
    code = ("import ast, pickle\n"
            "from transactional_runtime_healing.meter import snapshot, diff\n"
            "g = {'a': [1]}\n"
            "a = snapshot(None, g); b = snapshot(None, g)\n"
            "print(diff(a, b))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**__import__('os').environ,
                              "PYTHONPATH": __import__('os').path.join(
                                  __import__('os').path.dirname(__file__), "..", "src")})
    assert out.stdout.strip() == "[]", out.stdout + out.stderr


# ---- process-wide settings ---------------------------------------------------------------
import pytest  # noqa: E402


@pytest.mark.parametrize("name, change", [
    ("warnings.filters", lambda: __import__("warnings").filterwarnings("ignore", message="meter-test-unique")),
    ("logging.root.level", lambda: __import__("logging").getLogger().setLevel(50)),
    ("logging.disable", lambda: __import__("logging").disable(50)),
    ("random.state", lambda: __import__("random").seed(12345)),
    ("sys.recursionlimit", lambda: sys.setrecursionlimit(sys.getrecursionlimit() + 100)),
    ("socket.timeout", lambda: __import__("socket").setdefaulttimeout(7)),
    ("signal.handlers", lambda: __import__("signal").signal(__import__("signal").SIGUSR2, lambda *a: None)),
])
def test_process_wide_setting_changes_are_seen(name, change):
    import logging
    import random
    import signal
    import socket
    import warnings
    saved = (list(warnings.filters), logging.getLogger().level, logging.root.manager.disable,
             random.getstate(), sys.getrecursionlimit(), socket.getdefaulttimeout(),
             signal.getsignal(signal.SIGUSR2))
    try:
        before = snap({})
        change()
        assert f"@setting:{name}" in diff(before, snap({}))
    finally:
        warnings.filters[:] = saved[0]
        logging.getLogger().setLevel(saved[1])
        logging.disable(saved[2])
        random.setstate(saved[3])
        sys.setrecursionlimit(saved[4])
        socket.setdefaulttimeout(saved[5])
        signal.signal(signal.SIGUSR2, saved[6])


def test_level_of_a_named_logger_is_seen():
    import logging
    lg = logging.getLogger("meter.test.logger")
    old = lg.level
    try:
        before = snap({})
        lg.setLevel(40)
        assert "@setting:logging.loggers" in diff(before, snap({}))
    finally:
        lg.setLevel(old)


def test_replacing_sys_stdout_is_seen(monkeypatch):
    import io
    before = snap({})
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert "@setting:sys.stdout" in diff(before, snap({}))


def test_simplefilter_ignore_is_seen_although_the_meter_silences_its_own_warnings():
    import warnings
    with warnings.catch_warnings():
        warnings.resetwarnings()                  # a program with no filter yet
        before = snap({})
        warnings.simplefilter("ignore")
        assert "@setting:warnings.filters" in diff(before, snap({}))