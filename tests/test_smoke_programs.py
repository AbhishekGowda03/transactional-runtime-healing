"""Every shared-state program must (1) crash when run with no repair and (2) be accepted with its
reference repair, in plain and live mode. Checked without any model."""
import json
import os
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "experiments", "llm_smoke"))
import run_llm_smoke as R  # noqa: E402

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def run_with_rules(p, rules, mode):
    with tempfile.TemporaryDirectory() as d:
        lam = f"lambda: print('STATE', repr({p['state']}))"
        for name, text in p["setup"].items():
            open(os.path.join(d, name), "w").write(text)
        open(os.path.join(d, "prog.py"), "w").write(R.TEMPLATE.format(lam=lam, body=p["body"]))
        open(os.path.join(d, "rules.json"), "w").write(json.dumps(rules))
        out = subprocess.run([sys.executable, "-m", "transactional_runtime_healing.live_run",
                              os.path.join(d, "prog.py"), os.path.join(d, "rules.json"),
                              "--mode", mode], capture_output=True, text=True, cwd=d, timeout=60,
                             env=dict(os.environ, PYTHONPATH=SRC)).stdout
    state = next((l[6:] for l in out.splitlines() if l.startswith("STATE ")), None)
    return "original error" not in out, state


@pytest.mark.parametrize("p", R.SETS["shared"], ids=lambda p: p["id"])
def test_program_crashes_without_repair_and_reference_fix_is_accepted(p):
    proceeded, state = run_with_rules(p, [], "plain")
    assert not proceeded and state is not None, (state,)
    for mode in ("plain", "live"):
        proceeded, state = run_with_rules(p, [{"code": p["fix"]}], mode)
        assert proceeded and R.accepted(p, state), (mode, state)


def test_a_repair_that_changes_shared_state_and_then_fails_shows_as_damage_only_in_plain_mode():
    """The situation the shared set is meant to expose, built by hand: the repair writes into the
    shared queue and then raises. Plain mode keeps the write; the protocol discards it."""
    p = next(x for x in R.SETS["shared"] if x["id"] == "shared_queue")
    rules = [{"code": "queue.extend([7, 8])\nraise RuntimeError('repair failed')"}]
    clean = run_with_rules(p, [], "plain")
    plain = run_with_rules(p, rules, "plain")
    live = run_with_rules(p, rules, "live")
    assert not plain[0] and not live[0]                 # neither gets through
    assert plain[1] != clean[1] and "7, 8" in plain[1]  # plain: the queue was changed
    assert live[1] == clean[1]                          # protocol: exactly as before the repair


def test_polluting_repairs_are_judged_wrong_even_though_the_program_goes_on():
    cfg = next(x for x in R.SETS["shared"] if x["id"] == "shared_config")
    env = next(x for x in R.SETS["shared"] if x["id"] == "env_missing")
    ok1, s1 = run_with_rules(cfg, [{"code": "cfg['port'] = 8080\nport = 8080"}], "plain")
    ok2, s2 = run_with_rules(env, [{"code": "os.environ['SERVICE_URL'] = 'u'\nurl = 'u'"}], "plain")
    assert ok1 and not R.accepted(cfg, s1)
    assert ok2 and not R.accepted(env, s2)


def test_directory_hash_sees_an_empty_folder_and_ignores_the_runners_own_files(tmp_path):
    (tmp_path / "prog.py").write_text("x")                 # the runner's own file: ignored
    before = R.dir_hash(str(tmp_path))
    (tmp_path / "log.jsonl").write_text("y")
    assert R.dir_hash(str(tmp_path)) == before
    (tmp_path / "workdir").mkdir()                         # an empty folder: must show
    assert R.dir_hash(str(tmp_path)) != before