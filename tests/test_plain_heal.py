"""Tests for plain healing (HealBench-style baseline) and the comparison with the protocol."""
import json
import os
import subprocess
import sys
import textwrap

import pytest

from transactional_runtime_healing.instrument import run_instrumented
from transactional_runtime_healing.plain_heal import PlainHealer

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def plain(src, code_for, **kw):
    healer = PlainHealer(lambda ctx: code_for if isinstance(code_for, str) else None, **kw)
    g = run_instrumented(src, healer)
    return g, healer


def test_plain_applies_the_repair_and_continues():
    src = "def f(d):\n    v = d['x'] * 2\n    return v + 1\nresult = f({'y': 1})\n"
    g, h = plain(src, "v = 0")
    assert g["result"] == 1 and h.log[0].result.status.name == "COMMITTED"
    assert h.log[0].result.mode == "plain"


def test_failed_attempt_is_not_undone_and_residue_is_measured():
    src = "shared = [1]\ndef f():\n    v = 1 / 0\nf()\n"
    healer = PlainHealer(lambda ctx: "shared.append(2)\nraise ValueError('bad')" if "1 / 0" in ctx.statement else None,
                         meter_state=True)
    g = {}
    with pytest.raises(ZeroDivisionError):
        run_instrumented(src, healer, globals_=g)
    assert g["shared"] == [1, 2]                              # left behind, as in HealBench
    res = healer.log[0].result
    assert res.status.name == "REJECTED_REPAIR_ERROR" and res.residue == ["global:shared"]


def test_check_that_would_fail_is_reported_but_the_repair_is_applied():
    src = "d = {'n': 1}\ndef f():\n    v = 1 / 0\n    return v\nresult = f()\n"
    healer = PlainHealer(lambda ctx: "d['n'] = 'oops'\nv = 'text'" if "1 / 0" in ctx.statement else None,
                         checks=[lambda env: (isinstance(env['v'], int), "v must be an int")],
                         meter_state=True)
    g = run_instrumented(src, healer)
    res = healer.log[0].result
    assert g["result"] == "text" and g["d"] == {"n": "oops"}      # plain healing accepts it
    assert res.check_would_fail == "v must be an int"
    assert res.invalid_commit_changes == ["global:d"] and res.residue == []


def test_successful_repair_with_in_place_change_has_no_residue():
    src = "shared = [1]\ndef f():\n    v = 1 / 0\n    return v\nresult = f()\n"
    healer = PlainHealer(lambda ctx: "shared.append(2)\nv = 0" if "1 / 0" in ctx.statement else None,
                         meter_state=True)
    g = run_instrumented(src, healer)
    res = healer.log[0].result
    assert g["shared"] == [1, 2] and res.residue == [] and res.inplace_changes == ["global:shared"]


def test_timeout_is_reported():
    src = "def f():\n    v = 1 / 0\nf()\n"
    healer = PlainHealer(lambda ctx: "while True: pass" if "1 / 0" in ctx.statement else None,
                         timeout=1)
    with pytest.raises(ZeroDivisionError):
        run_instrumented(src, healer)
    assert healer.log[0].result.status.name == "REJECTED_TIMEOUT"


# ---- the same program under both modes ----------------------------------------------------
@pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")
def test_same_failing_repair_leaves_residue_in_plain_mode_and_none_in_live_mode(tmp_path):
    prog = tmp_path / "prog.py"
    prog.write_text(textwrap.dedent("""
        import sys
        shared = [1, 2, 3]
        sys.excepthook = lambda *a: print('shared =', shared)
        def f():
            v = 1 / 0
        f()
    """))
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps([{"match": "1 / 0", "code": "shared.append(99)\nraise ValueError('bad')"}]))
    out = {}
    for mode in ("plain", "live"):
        log = tmp_path / f"{mode}.jsonl"
        p = subprocess.run([sys.executable, "-m", "transactional_runtime_healing.live_run",
                            str(prog), str(rules), "--mode", mode, "--meter", "--log", str(log)],
                           capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=SRC),
                           cwd=tmp_path, timeout=60)
        entries = [json.loads(l) for l in log.read_text().splitlines()]
        out[mode] = (p.stdout.strip(), [e for e in entries if e["status"] != "NO_PROPOSAL"][0])
    assert out["plain"][0] == "shared = [1, 2, 3, 99]" and out["plain"][1]["residue"] == ["global:shared"]
    assert out["live"][0] == "shared = [1, 2, 3]" and out["live"][1]["residue"] == []
    assert out["plain"][1]["mode"] == "plain" and out["live"][1]["mode"] == "live"


def test_files_written_by_a_failed_plain_attempt_are_external_residue(tmp_path):
    cache = tmp_path / "cache.txt"
    src = "def f():\n    v = 1 / 0\nf()\n"
    code = f"open({str(cache)!r}, 'w').write('x')\nraise ValueError('bad')"
    healer = PlainHealer(lambda ctx: code if "1 / 0" in ctx.statement else None)
    with pytest.raises(ZeroDivisionError):
        run_instrumented(src, healer)
    assert cache.exists()
    assert healer.log[0].result.external_residue == [f"open-write:{cache}"]


def test_programs_own_timer_survives_a_plain_attempt():
    import signal
    seen = []
    old = signal.signal(signal.SIGALRM, lambda *a: seen.append(1))
    try:
        signal.setitimer(signal.ITIMER_REAL, 30)
        g, _ = plain("def f():\n    v = 1 / 0\n    return v\nresult = f()\n", "v = 0")
        delay, _ = signal.getitimer(signal.ITIMER_REAL)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
    assert g["result"] == 0 and 0 < delay <= 30


@pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")
def test_file_written_by_a_rejected_live_trial_is_external_residue_although_memory_is_clean(tmp_path):
    cache = tmp_path / "cache.txt"
    prog = tmp_path / "prog.py"
    prog.write_text(textwrap.dedent("""
        import sys
        sys.excepthook = lambda *a: print('original error')
        def f():
            v = 1 / 0
        f()
    """))
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps([{"match": "1 / 0", "code":
                                  f"open({str(cache)!r}, 'w').write('x')\nraise ValueError('bad')"}]))
    log = tmp_path / "live.jsonl"
    subprocess.run([sys.executable, "-m", "transactional_runtime_healing.live_run", str(prog),
                    str(rules), "--meter", "--log", str(log)], capture_output=True, text=True,
                   env=dict(os.environ, PYTHONPATH=SRC), cwd=tmp_path, timeout=60)
    entry = [json.loads(l) for l in log.read_text().splitlines() if "REJECTED" in l][0]
    assert cache.exists()
    assert entry["residue"] == [] and entry["external_residue"] == [f"open-write:{cache}"]
    assert entry["inplace_changes"] is None                  # not measured in live mode


def test_plain_mode_records_and_carries_out_external_effects(tmp_path):
    marker = tmp_path / "marker"
    src = "def f():\n    v = 1 / 0\nf()\n"
    code = f"import os\nos.system('touch {marker}')\nraise ValueError('bad')"
    healer = PlainHealer(lambda ctx: code if "1 / 0" in ctx.statement else None)
    with pytest.raises(ZeroDivisionError):
        run_instrumented(src, healer)
    assert marker.exists()                                   # it really happened
    assert "os.system" in healer.log[0].result.external_residue


@pytest.mark.skipif(sys.version_info < (3, 12), reason="needs sys.monitoring")
def test_live_mode_blocks_the_same_effect_so_it_is_not_residue(tmp_path):
    marker = tmp_path / "marker"
    prog = tmp_path / "prog.py"
    prog.write_text("import sys\nsys.excepthook = lambda *a: print('original error')\n"
                    "def f():\n    v = 1 / 0\nf()\n")
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps([{"match": "1 / 0", "code":
                                  f"import os\nos.system('touch {marker}')\nraise ValueError('bad')"}]))
    log = tmp_path / "live.jsonl"
    subprocess.run([sys.executable, "-m", "transactional_runtime_healing.live_run", str(prog),
                    str(rules), "--log", str(log)], capture_output=True, text=True,
                   env=dict(os.environ, PYTHONPATH=SRC), cwd=tmp_path, timeout=60)
    entry = [json.loads(l) for l in log.read_text().splitlines() if "REJECTED" in l][0]
    assert not marker.exists()
    assert entry["status"] == "REJECTED_EXTERNAL_EFFECT" and entry["external_residue"] == []