"""The completeness check: a repair has to bind what the failing statement would have bound."""
import json
import os
import subprocess
import sys
import textwrap

from transactional_runtime_healing.heal_point import missing_targets

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def test_missing_targets_cases():
    m = missing_targets
    assert m("r = a / b", {"a": 1}, {}, {"a": 1, "r": 0}, "r = 0") == []
    assert m("r = a / b", {"a": 1}, {}, {"a": 1, "_rh_rtn": 0}, "_rh_rtn = 0") == ["r"]
    assert m("a, b = pair", {}, {}, {"a": 1}, "a = 1") == ["b"]
    assert m("return x / y", {"_eh_rtn": None}, {}, {}, "_eh_rtn = 0") == []
    assert m("return x / y", {"_eh_rtn": None}, {}, {}, "_rtn = 0") == ["_eh_rtn"]
    assert m("r = f()", {"r": 5}, {}, {"r": 5}, "pass") == []          # old value stays bound
    assert m("t += prices[item]", {"t": 0}, {}, {"t": 0}, "pass") == []   # nothing required
    assert m("not python (", {}, {}, {}, "x = 1") == []                  # unparsable: no demand


def run(tmp_path, mode_args, repair):
    prog = tmp_path / "prog.py"
    prog.write_text(textwrap.dedent("""
        import sys
        sys.excepthook = lambda *a: print('original error')
        def rate(hits, total):
            r = hits / total
            return r
        print('result', rate(5, 0))
        """))
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps([{"code": repair}]))
    log = tmp_path / "log.jsonl"
    log.write_text("")
    out = subprocess.run([sys.executable, "-m", "transactional_runtime_healing.live_run",
                          str(prog), str(rules), "--log", str(log), *mode_args],
                         capture_output=True, text=True, cwd=tmp_path, timeout=60,
                         env=dict(os.environ, PYTHONPATH=SRC))
    entries = [json.loads(x) for x in log.read_text().splitlines()]
    return out.stdout, [e for e in entries if e["status"] not in ("NO_PROPOSAL", "SKIPPED")]


def test_noop_repair_is_committed_without_the_check_and_rejected_with_it(tmp_path):
    out, ent = run(tmp_path, ["--mode", "live"], "_rh_rtn = 0")
    assert ent[0]["status"] == "COMMITTED" and ent[0]["targets_missing"] == ["r"]
    out, ent = run(tmp_path, ["--mode", "live", "--require-targets"], "_rh_rtn = 0")
    assert ent[0]["status"] == "REJECTED_CHECK" and "does not bind r" in ent[0]["reason"]
    assert "original error" in out
    out, ent = run(tmp_path, ["--mode", "live", "--require-targets"], "r = 0")
    assert ent[0]["status"] == "COMMITTED" and ent[0]["targets_missing"] == []
    assert "result 0" in out


def test_plain_mode_records_the_missing_target_but_applies_the_repair(tmp_path):
    out, ent = run(tmp_path, ["--mode", "plain"], "_rh_rtn = 0")
    assert ent[0]["status"] == "COMMITTED" and ent[0]["targets_missing"] == ["r"]