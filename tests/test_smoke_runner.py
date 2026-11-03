"""The smoke runner, end to end, against a fake model that makes the mistake the real one made."""
import json
import os
import subprocess
import sys

from test_llm import Fake

ROOT = os.path.join(os.path.dirname(__file__), "..")


def run_runner(tmp_path, f, *extra, env_extra=None):
    env = dict(os.environ, TXH_API_BASE=f.url, TXH_MODEL="m")
    env.update(env_extra or {})
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, os.path.join(ROOT, "experiments", "llm_smoke", "run_llm_smoke.py"),
                        "--only", "zero_division", "--cache-dir", str(tmp_path / "c"),
                        "--out", str(out), *extra],
                       capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(open(str(out) + ".json").read()), open(str(out) + ".md").read(), r.stdout


def test_noop_repair_is_flagged_and_rejected_by_the_targets_check(tmp_path):
    f = Fake(["```python\n_rh_rtn = 0\n```"])
    try:
        data, md, _ = run_runner(tmp_path, f, "--settings", "plain,full,full+targets")
        rows = {r["setting"]: r for r in data["rows"]}
        # the no-op repair at `r = hits / total` leaves r unbound; the UnboundLocalError at
        # `return r` is the next crash and the same kind of repair is committed again
        assert rows["plain"]["noop"] == 2 and rows["full"]["noop"] == 2
        assert rows["full+targets"]["outcomes"] == ["REJECTED_CHECK"] and rows["full+targets"]["noop"] == 0
        assert not rows["full+targets"]["proceeded"]
        assert len(f.requests) == 2          # two distinct prompts (two crashes), then replays
    finally:
        f.close()


def test_runs_need_a_temperature_and_a_dead_model_is_reported_as_an_error(tmp_path):
    f = Fake(["```python\nr = 0\n```"])
    try:
        bad = subprocess.run([sys.executable, os.path.join(ROOT, "experiments", "llm_smoke", "run_llm_smoke.py"),
                              "--runs", "2", "--temperature", "0", "--only", "zero_division"],
                             capture_output=True, text=True, env=dict(os.environ, TXH_API_BASE=f.url))
        assert bad.returncode != 0 and "identical answers" in bad.stderr
    finally:
        f.close()
    other = Fake(["x"])
    try:                                              # nothing listens on port 9
        data, md, _ = run_runner(tmp_path, other, "--settings", "plain",
                                 env_extra=dict(TXH_API_BASE="http://127.0.0.1:9/v1"))
    finally:
        other.close()
    assert data["rows"][0]["model_error"] and "WARNING" in md


def test_call_cap_holds_across_processes(tmp_path):
    f = Fake(["```python\nr = 0\n```"])
    try:
        data, md, _ = run_runner(tmp_path, f, "--settings", "plain", "--max-calls", "0")
        assert len(f.requests) == 0 and data["rows"][0]["replies"].get("budget_exhausted", 0) >= 1
    finally:
        f.close()