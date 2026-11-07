"""repo_hook + pytest plugin on a small toy repository, in plain and live mode."""
import json
import os
import subprocess
import sys
import textwrap

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


@pytest.fixture
def toy(tmp_path):
    repo = tmp_path / "repo"
    (repo / "toypkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "toypkg" / "__init__.py").write_text("")
    (repo / "toypkg" / "core.py").write_text(textwrap.dedent("""
        PRICES = {'a': 1, 'b': 2}
        def total(items):
            t = 0
            for i in items:
                t += PRICES[i]
            return t
        def rate(x, y):
            r = x / y
            return r
        """))
    (repo / "tests" / "test_core.py").write_text(textwrap.dedent("""
        from toypkg.core import total, rate
        def test_total():
            assert total(['a', 'zzz', 'b']) == 3
        def test_rate():
            assert rate(4, 0) == 0
        """))
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps([{"match": "PRICES[i]", "code": "t += 0"},
                                 {"match": "x / y", "code": "r = 0"}]))
    return repo, rules


def run_pytest(repo, rules, tmp_path, mode, extra_env=None):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(repo), SRC]), TXH_HEAL_ROOT=str(repo),
               TXH_HEAL_MODE=mode, TXH_HEAL_RULES=str(rules), TXH_HEAL_LOG=str(tmp_path / "log.jsonl"),
               TXH_HEAL_CACHE=str(tmp_path / "cache"), PYTHONDONTWRITEBYTECODE="1")
    env.update(extra_env or {})
    if (tmp_path / "log.jsonl").exists():
        (tmp_path / "log.jsonl").unlink()
    p = subprocess.run([sys.executable, "-m", "pytest", "-p", "transactional_runtime_healing.pytest_plugin",
                        "-s", "-q", "-p", "no:cacheprovider", "tests"], cwd=repo, env=env,
                       capture_output=True, text=True, timeout=120)
    log = []
    if (tmp_path / "log.jsonl").exists():
        log = [json.loads(x) for x in (tmp_path / "log.jsonl").read_text().splitlines()]
    return p, [e for e in log if e["status"] not in ("NO_PROPOSAL", "SKIPPED")
               and not e["status"].startswith("post-commit")]


@pytest.mark.parametrize("mode", ["plain", "live"])
def test_tests_fail_without_healing_and_pass_with_it(toy, tmp_path, mode):
    repo, rules = toy
    off = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
                         cwd=repo, env=dict(os.environ, PYTHONPATH=str(repo)),
                         capture_output=True, text=True)
    assert "2 failed" in off.stdout
    p, log = run_pytest(repo, rules, tmp_path, mode)
    assert "2 passed" in p.stdout, p.stdout + p.stderr
    assert "modules instrumented" in p.stdout and len(log) == 2
    assert all(e["status"] == "COMMITTED" for e in log)


def test_second_run_uses_the_cache_and_tests_are_not_instrumented(toy, tmp_path):
    repo, rules = toy
    run_pytest(repo, rules, tmp_path, "plain")
    assert any(f.endswith(".pkl") for f in os.listdir(tmp_path / "cache"))
    p, log = run_pytest(repo, rules, tmp_path, "plain")
    assert "2 passed" in p.stdout
    # toypkg/__init__.py and core.py are instrumented; tests/test_core.py is not
    assert "2 modules instrumented" in p.stdout


def test_wanted_excludes_tests_docs_and_vendor(tmp_path):
    from transactional_runtime_healing.repo_hook import wanted
    root = tmp_path
    for rel in ["pkg/a.py", "pkg/tests/test_a.py", "docs/conf.py", "pkg/_vendor/x.py",
                "pkg/test_b.py", "conftest.py", "setup.py", "pkg/c.txt"]:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x=1")
    got = {rel for rel in ["pkg/a.py", "pkg/tests/test_a.py", "docs/conf.py", "pkg/_vendor/x.py",
                           "pkg/test_b.py", "conftest.py", "setup.py", "pkg/c.txt"]
           if wanted(str(root / rel), str(root))}
    assert got == {"pkg/a.py"}


def test_exceptions_the_test_expects_are_not_healed(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pk").mkdir(parents=True); (repo / "tests").mkdir()
    (repo / "pk" / "__init__.py").write_text("")
    (repo / "pk" / "m.py").write_text("def f(x):\n    y = 1 / x\n    return y\n")
    (repo / "tests" / "test_m.py").write_text(
        "import pytest\nfrom pk.m import f\n"
        "def test_expected():\n    with pytest.raises(ZeroDivisionError):\n        f(0)\n"
        "def test_expected_call():\n    pytest.raises(ZeroDivisionError, lambda: f(0))\n")
    rules = tmp_path / "r.json"; rules.write_text(json.dumps([{"match": "1 / x", "code": "y = 0"}]))
    p, log = run_pytest(repo, rules, tmp_path, "plain")
    assert "2 passed" in p.stdout and log == [], p.stdout


def test_module_that_cannot_be_instrumented_is_loaded_unchanged_and_remembered(toy, tmp_path):
    repo, rules = toy
    (repo / "toypkg" / "deep.py").write_text("x = " + " + ".join(["0"] * 2500) + " + 1\n")
    (repo / "tests" / "test_deep.py").write_text("from toypkg.deep import x\ndef test_x():\n    assert x == 1\n")
    p, _ = run_pytest(repo, rules, tmp_path, "plain")
    assert "left unchanged" in p.stdout and "3 passed" in p.stdout, p.stdout + p.stderr


def test_nothing_is_healed_outside_test_bodies(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pk").mkdir(parents=True); (repo / "tests").mkdir()
    (repo / "pk" / "__init__.py").write_text("")
    (repo / "pk" / "m.py").write_text("X = 1 / 0\n")             # crashes at import time
    (repo / "tests" / "test_m.py").write_text("def test_x():\n    from pk.m import X\n"
                                              "    assert X == 0\n")
    rules = tmp_path / "r.json"; rules.write_text(json.dumps([{"match": "1 / 0", "code": "X = 0"}]))
    p, log = run_pytest(repo, rules, tmp_path, "plain")
    assert "1 passed" in p.stdout and len(log) == 1             # imported inside the test: healed
    (repo / "tests" / "test_m.py").write_text("from pk.m import X\ndef test_x():\n    assert X == 0\n")
    p, log = run_pytest(repo, rules, tmp_path, "plain")
    assert "error" in p.stdout.lower() and not any(e["status"] == "COMMITTED" for e in log)         # imported at collection: not healed
    p, log = run_pytest(repo, rules, tmp_path, "plain", {"TXH_HEAL_ALL": "1"})
    assert "1 passed" in p.stdout and len(log) == 1