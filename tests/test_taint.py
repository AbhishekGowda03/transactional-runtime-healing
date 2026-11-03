"""Tests for the protected-operations check (proposal check 2)."""
import json
import os
import subprocess
import sys
import textwrap

import pytest

from transactional_runtime_healing.instrument import run_instrumented
from transactional_runtime_healing.plain_heal import PlainHealer
from transactional_runtime_healing.taint import TaintGuard, leaves

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
needs312 = pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")


# ---- the guard on its own ---------------------------------------------------------------
def test_leaves_collects_strings_paths_and_nested_values():
    import pathlib

    class Box:
        def __init__(self):
            self.name = "inside-object"
    state = [{"k": ["nested-string"]}, ("tuple-string",), pathlib.Path("/data/file.csv"),
             b"bytes-value", Box(), 12345, "abc"]
    found = leaves([state])
    assert {"nested-string", "tuple-string", "/data/file.csv", "bytes-value",
            "inside-object"} <= found
    assert "abc" not in found                                # shorter than min_len


def test_operation_by_the_healing_code_is_healing_influenced():
    g = TaintGuard()
    g.start([{"a": "x"}])
    assert "healing code itself" in g("os.remove", ("/tmp/x", None))


def test_value_introduced_by_the_repair_is_recognised_later():
    g = TaintGuard()
    g.start([{"existing": "/data/keep.csv"}])
    g.finish([{"existing": "/data/keep.csv"}], {"target": "/data/old/important.csv"})
    assert g("os.remove", ("/data/old/important.csv", None))          # equal
    assert g("os.remove", ("/backup" + "/data/old/important.csv", None))   # contains
    assert g("os.rename", ("/data/old/important.csv", "/x"))
    assert g("os.remove", ("/data/keep.csv", None)) is None           # was there before


def test_path_component_match():
    g = TaintGuard()
    g.start([{}])
    g.finish([{}], {"name": "secret_report"})
    assert g("open", ("/srv/out/secret_report/data.txt", "w", 0))


def test_unrelated_operations_are_not_flagged():
    g = TaintGuard()
    g.start([{}])
    g.finish([{}], {"value": "healed-default"})
    assert g("os.remove", ("/tmp/other-file.txt", None)) is None


def test_safe_paths_are_not_policed():
    g = TaintGuard(safe_paths=["/tmp/scratch"])
    g.start([{}])
    assert g("open", ("/tmp/scratch/x.txt", "w", 0)) is None


# ---- plain mode with the guard ("HealGuard-style") ----------------------------------------
def test_plain_guard_stops_a_deletion_by_the_repair(tmp_path):
    victim = tmp_path / "important.txt"
    victim.write_text("keep me")
    src = "def f():\n    v = 1 / 0\n    return v\nf()\n"
    code = f"import os\nos.remove({str(victim)!r})\nv = 0"
    healer = PlainHealer(lambda ctx: code if "1 / 0" in ctx.statement else None, guard=True)
    with pytest.raises(ZeroDivisionError):
        run_instrumented(src, healer)
    assert victim.exists()
    assert healer.log[0].result.status.name == "REJECTED_PROTECTED_OPERATION"


def test_plain_guard_also_catches_a_swallowed_attempt(tmp_path):
    victim = tmp_path / "important.txt"
    victim.write_text("keep me")
    src = "def f():\n    v = 1 / 0\n    return v\nf()\n"
    code = (f"import os\ntry:\n    os.remove({str(victim)!r})\nexcept Exception:\n    pass\nv = 0")
    healer = PlainHealer(lambda ctx: code if "1 / 0" in ctx.statement else None, guard=True)
    with pytest.raises(ZeroDivisionError):
        run_instrumented(src, healer)
    assert victim.exists()


def test_plain_without_guard_carries_out_the_deletion(tmp_path):
    victim = tmp_path / "important.txt"
    victim.write_text("gone")
    src = "def f():\n    v = 1 / 0\n    return v\nresult = f()\n"
    code = f"import os\nos.remove({str(victim)!r})\nv = 0"
    healer = PlainHealer(lambda ctx: code if "1 / 0" in ctx.statement else None)
    g = run_instrumented(src, healer)
    assert not victim.exists() and g["result"] == 0


# ---- live mode ----------------------------------------------------------------------------
def run_live(tmp_path, program, rules, *args):
    prog = tmp_path / "prog.py"
    prog.write_text("import sys\nsys.excepthook = lambda *a: print('original error')\n"
                    + textwrap.dedent(program))
    rul = tmp_path / "rules.json"
    rul.write_text(json.dumps(rules))
    log = tmp_path / "log.jsonl"
    p = subprocess.run([sys.executable, "-m", "transactional_runtime_healing.live_run",
                        str(prog), str(rul), "--log", str(log), *args],
                       capture_output=True, text=True, timeout=60, cwd=tmp_path,
                       env=dict(os.environ, PYTHONPATH=SRC))
    entries = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    return p, [e for e in entries if e["status"] not in ("NO_PROPOSAL", "SKIPPED")]


@needs312
def test_repair_that_deletes_a_file_is_rejected_and_the_file_survives(tmp_path):
    victim = tmp_path / "important.txt"
    victim.write_text("keep me")
    p, log = run_live(tmp_path, """
        def f():
            v = 1 / 0
        f()
    """, [{"match": "1 / 0", "code": f"import os\nos.remove({str(victim)!r})\nv = 0"}])
    assert victim.exists() and p.stdout.strip() == "original error"
    assert log[0]["status"] == "REJECTED_PROTECTED_OPERATION" and log[0]["category"] == "safety"


@needs312
def test_continuation_that_deletes_a_path_the_repair_supplied_is_rejected(tmp_path):
    victim = tmp_path / "important.txt"
    victim.write_text("keep me")
    p, log = run_live(tmp_path, f"""
        import os
        def f(path):
            target = path['dir']                    # KeyError: the repair supplies the target
            os.remove(target)
        f({{}})
    """, [{"match": "path['dir']", "code": f"target = {str(victim)!r}"}])
    assert victim.exists() and p.stdout.strip() == "original error"
    assert log[0]["status"] == "REJECTED_PROTECTED_OPERATION"
    assert "introduced by the healing code" in log[0]["reason"]


@needs312
def test_continuation_that_deletes_its_own_file_is_allowed(tmp_path):
    own = tmp_path / "scratch.tmp"
    own.write_text("x")
    p, log = run_live(tmp_path, f"""
        import os
        def f(d):
            v = d['x']
            os.remove({str(own)!r})
            return v
        print(f({{}}))
    """, [{"match": "d['x']", "code": "v = 'healed-value'"}])
    assert not own.exists() and p.stdout.strip() == "healed-value"
    assert log[0]["status"] == "COMMITTED"


@needs312
def test_guard_can_be_switched_off(tmp_path):
    victim = tmp_path / "important.txt"
    victim.write_text("x")
    p, log = run_live(tmp_path, """
        def f():
            v = 1 / 0
        f()
    """, [{"match": "1 / 0", "code": f"import os\nos.remove({str(victim)!r})\nv = 0"}], "--no-guard")
    assert not victim.exists() and log[0]["status"] == "COMMITTED"


def test_plain_guard_stops_a_command_that_record_only_mode_would_let_through(tmp_path):
    marker = tmp_path / "marker"
    src = "def f():\n    v = 1 / 0\n    return v\nf()\n"
    code = f"import os\nos.system('touch {marker}')\nv = 0"
    healer = PlainHealer(lambda ctx: code if "1 / 0" in ctx.statement else None, guard=True)
    with pytest.raises(ZeroDivisionError):
        run_instrumented(src, healer)
    assert not marker.exists()
    assert healer.log[0].result.status.name == "REJECTED_PROTECTED_OPERATION"


def test_an_operation_stopped_by_the_guard_is_not_listed_as_carried_out():
    src = "def f():\n    v = 1 / 0\nf()\n"
    healer = PlainHealer(lambda ctx: "import os\nos.system('true')" if "1 / 0" in ctx.statement
                         else None, guard=True)
    with pytest.raises(ZeroDivisionError):
        run_instrumented(src, healer)
    assert healer.log[0].result.external_residue == []


# ---- third review ------------------------------------------------------------------
def test_short_common_word_does_not_reject_a_safe_write():
    g = TaintGuard()
    g.start([{}])
    g.finish([{}], {"default": "data"})
    assert g("open", ("/tmp/out/data.txt", "w", 0)) is None
    assert g("os.remove", ("/tmp/metadata/x", None)) is None


def test_long_value_matches_only_as_a_whole_token():
    g = TaintGuard()
    g.start([{}])
    g.finish([{}], {"v": "important_dir"})
    assert g("os.remove", ("/x/important_dir/y", None))
    assert g("os.remove", ("/x/not_important_dir_2/y", None)) is None


def test_lineage_is_inherited():
    g = TaintGuard(inherit={"/etc/secret.conf"})
    g.start([{}])
    g.finish([{}])
    assert g("os.remove", ("/etc/secret.conf", None))


def test_custom_event_list_and_truncation_flag():
    g = TaintGuard(events={"os.remove"})
    assert "os.mkdir" not in g.events
    from transactional_runtime_healing import taint
    big = [{"k": ["abcdef%d" % i for i in range(30)]}]
    old = taint._MAX_NODES
    taint._MAX_NODES = 10
    try:
        g.start(big)
    finally:
        taint._MAX_NODES = old
    assert g.truncated


def test_breadth_first_covers_every_root_under_a_node_cap():
    from transactional_runtime_healing import taint
    old = taint._MAX_NODES
    taint._MAX_NODES = 6
    try:
        out = taint.leaves([{"a": "first_root_value"}, {"b": "second_root_value"}])
    finally:
        taint._MAX_NODES = old
    assert {"first_root_value", "second_root_value"} <= out