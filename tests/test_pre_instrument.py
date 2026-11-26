"""Pre-instrumentation for Python 3.7 (pre_instrument.py, repo_hook's pre-instrumented loader).

Most tests use a stored entry (tests/fixtures/pre_instrument/sample_mod.ast.z) and run on every
Python version, including 3.7 inside a task image. The tests that build entries need Python 3.9+.
"""
import ast
import json
import os
import subprocess
import sys
import textwrap
import traceback

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
FIX = os.path.join(HERE, "fixtures", "pre_instrument")
ENTRY = os.path.join(FIX, "sample_mod.ast.z")
NAME = "/fixture/sample_mod.py"
CAN_BUILD = sys.version_info >= (3, 9)

sys.path.insert(0, SRC)
from transactional_runtime_healing import instrument as ins  # noqa: E402
from transactional_runtime_healing import pre_instrument as pi  # noqa: E402


def source():
    with open(os.path.join(FIX, "sample_mod.py"), encoding="utf-8") as f:
        return f.read()


def Result(bindings):
    from transactional_runtime_healing.heal_point import HealResult
    return HealResult(status="COMMITTED", reason="test", bindings=bindings)


def load(stub):
    code, table, reads, spans = pi.load_entry(ENTRY, use_cache=False)
    mod = {"__name__": "sample_mod", "__heal__": stub, "__heal_apply__": ins._apply}
    exec(code, mod)
    return mod, table, reads, spans


def plain():
    mod = {"__name__": "sample_mod"}
    exec(compile(source(), NAME, "exec"), mod)
    return mod


def no_heal(exc, loc, glob, site):
    return None


def test_the_entry_runs_like_the_original_program():
    mod, _, _, _ = load(no_heal)
    ref = plain()
    assert mod["__doc__"] == ref["__doc__"] and mod["__doc__"]
    for name, args in [("slices", ([10, 20, 30, 40],)), ("table", (["a", "b"],)), ("ratio", (6, 3)),
                       ("fstring", ("x", 7)), ("nested", (6,)), ("guarded", (0,)), ("guarded", (5,)),
                       ("condition", (4,)), ("condition", (-1,)), ("condition", (10 ** 6,))]:
        assert mod[name](*args) == ref[name](*args), name
    assert [mod["bump"](), mod["bump"]()] == [1, 2]
    assert mod["Box"].make(4).twice == 8 and mod["Box"].kind == "box"
    for const in ("BIG", "COMPLEX", "RAW", "NOTHING", "INF"):
        assert mod[const] == ref[const], const
    assert mod["NAN_LIKE"] == float("inf")
    import asyncio
    assert asyncio.get_event_loop().run_until_complete(mod["acoro"](1)) == 2
    assert mod["Box"].twice.__doc__ == ref["Box"].twice.__doc__


def test_a_crash_calls_the_healer_with_the_original_line_numbers():
    seen = []

    def healer(exc, loc, glob, site):
        seen.append((type(exc).__name__, site[0], site[1], site[2], site[3]))
        return Result({"r": 0}) if site[2] == "ratio" else Result({})

    mod, _, _, _ = load(healer)
    assert mod["ratio"](4, 0) == 0                       # r = 0 supplied by the "repair"
    assert mod["table"](["a", "zzz", "b"]) == 3          # the failing statement is skipped
    lines = source().splitlines()
    (kind, filename, lineno, func, segment) = seen[0]
    assert (kind, filename, func) == ("ZeroDivisionError", NAME, "ratio")
    assert lines[lineno - 1].strip() == "r = x / y" and segment == "r = x / y"
    assert seen[1][3:] == ("table", "total += PRICES[i]")


def test_a_traceback_points_at_the_original_line():
    mod, _, _, _ = load(no_heal)
    with pytest.raises(ZeroDivisionError) as e:
        mod["ratio"](1, 0)
    frames = traceback.extract_tb(e.value.__traceback__)
    f = [x for x in frames if x.name == "ratio"][-1]
    assert f.filename == NAME and source().splitlines()[f.lineno - 1].strip() == "r = x / y"


def test_handler_table_reads_and_function_spans():
    _, table, reads, spans = load(no_heal)
    lines = source().splitlines()
    start = next(i for i, l in enumerate(lines, 1) if l.startswith("def ratio"))
    assert (start, start + 2) in spans
    guarded = next(i for i, l in enumerate(lines, 1) if "return 10 / x" in l)
    assert ins._handler_types_at(table, guarded) == ["ZeroDivisionError"]
    assert {"PRICES", "math"} <= reads


def test_function_source_uses_the_stored_spans(tmp_path):
    from transactional_runtime_healing import llm
    path = tmp_path / "m.py"
    path.write_text("def a():\n    x = 1\n    return x\n\n\ndef b():\n    y = 2\n    return y\n")
    ins.FUNC_SPANS[str(path)] = [(1, 3), (6, 8)]
    try:
        assert llm._function_source(str(path), 7) == "def b():\n    y = 2\n    return y"
    finally:
        del ins.FUNC_SPANS[str(path)]
    assert llm._function_source(str(path), 2) == "def a():\n    x = 1\n    return x"   # no spans: by the tree


def test_end_by_indent_matches_the_tree_on_this_sample():
    from transactional_runtime_healing import llm
    lines = source().splitlines()
    tree = ast.parse(source())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and hasattr(node, "end_lineno"):
            assert llm._end_by_indent(lines, node.lineno) == node.end_lineno, node.name


def test_the_compiled_copy_is_kept_and_reused(tmp_path):
    import shutil
    entry = tmp_path / "e.ast.z"
    shutil.copy(ENTRY, entry)
    a = pi.load_entry(str(entry))
    kept = [n for n in os.listdir(tmp_path) if n.endswith(".code")]
    assert len(kept) == 1
    b = pi.load_entry(str(entry))
    assert a[1] == b[1] and a[2] == b[2] and a[3] == b[3]
    assert pi.warm(str(tmp_path)) == (1, 0, 0)            # nothing left to decode


def test_all_sources_are_valid_python_37():
    """The package must import on 3.7: parse every file with the 3.7 grammar."""
    if sys.version_info < (3, 8):
        pytest.skip("feature_version needs Python 3.8+")
    bad = []
    for name in os.listdir(os.path.join(SRC, "transactional_runtime_healing")):
        if name.endswith(".py"):
            path = os.path.join(SRC, "transactional_runtime_healing", name)
            try:
                ast.parse(open(path, encoding="utf-8").read(), path, feature_version=(3, 7))
            except SyntaxError as e:
                bad.append("%s:%s %s" % (name, e.lineno, e.msg))
    assert not bad, bad


# ---- tests that build entries (host, Python 3.9+) ----------------------------------------------
@pytest.mark.skipif(not CAN_BUILD, reason="instrumenting needs Python 3.9+")
def test_the_stored_entry_is_current():
    """If this fails the instrumenter changed: run `python tests/fixtures/pre_instrument/make.py`."""
    fresh = pi.loads(pi.build_entry(source().encode("utf-8"), NAME))
    stored = pi.loads(open(ENTRY, "rb").read())
    for key in ("table", "reads", "spans", "filename"):
        assert fresh[key] == stored[key], key
    # the stored entry may come from another Python version (other node fields, e.g. type_params)
    fields_of = lambda d: {n: f for n, f, _ in d["classes"]}  # noqa: E731
    f1, f2 = fields_of(fresh), fields_of(stored)

    def canon(v, classes, other):
        if isinstance(v, list):
            return [canon(x, classes, other) for x in v]
        if isinstance(v, dict) and "c" in v:
            name, fields, _ = classes[v["c"]]
            use = [i for i, f in enumerate(fields) if f in other.get(name, fields)]
            # columns are left out (nodes inside f-strings have other columns before 3.12); alias
            # and keyword got positions in 3.10
            pos = (None,) if name in ("alias", "keyword") else (v.get("l"),)
            return (name, [canon(v["f"][i], classes, other) for i in use]) + pos
        return v
    assert canon(fresh["tree"], fresh["classes"], f2) == canon(stored["tree"], stored["classes"], f1)


@pytest.mark.skipif(not CAN_BUILD, reason="instrumenting needs Python 3.9+")
def test_a_round_trip_gives_the_same_tree():
    tree, table, reads, spans = ins.instrument_tree(source(), NAME, feature_version=(3, 7))
    back = pi.tree_from_data(pi.to_data(tree, table, reads, spans, NAME))
    assert ast.dump(back) == ast.dump(tree)               # same nodes and fields
    pos = lambda t: [(n.lineno, n.col_offset) for n in ast.walk(t) if hasattr(n, "lineno")]  # noqa: E731
    assert pos(back) == pos(tree)                         # same line numbers


@pytest.mark.skipif(sys.version_info < (3, 10), reason="feature_version is enforced fully from 3.10 on")
def test_a_file_python_37_cannot_parse_is_not_instrumented():
    with pytest.raises(SyntaxError):
        pi.build_entry(b"def f(a, /):\n    return a\n", "/x/f.py")
    with pytest.raises(SyntaxError):
        pi.build_entry(b"if (n := 3) > 2:\n    pass\n", "/x/g.py")


@pytest.mark.skipif(not CAN_BUILD, reason="instrumenting needs Python 3.9+")
def test_segments_equal_ast_get_source_segment():
    for src in ["x = 'é'\r\nif a:\r  y = 1\n\nz = (1,\n 2)\r\n", "a=1", "\n\nb = 'ü' + c\n", source()]:
        seg = ins._Segments(src)
        for n in ast.walk(ast.parse(src)):
            if hasattr(n, "end_lineno"):
                assert seg.get(n) == ast.get_source_segment(src, n)


# ---- a small repository, loaded through the pre-instrumented path ------------------------------
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


def run_toy(repo, rules, tmp_path, extra_env):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(repo), SRC]), TXH_HEAL_ROOT=str(repo),
               TXH_HEAL_MODE="live", TXH_HEAL_RULES=str(rules), TXH_HEAL_LOG=str(tmp_path / "log.jsonl"),
               TXH_HEAL_CACHE=str(tmp_path / "cache"), PYTHONDONTWRITEBYTECODE="1")
    env.update(extra_env)
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "transactional_runtime_healing.pytest_plugin",
                           "-s", "-q", "-p", "no:cacheprovider", "tests"], cwd=str(repo), env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=180)


@pytest.mark.skipif(not CAN_BUILD, reason="instrumenting needs Python 3.9+")
def test_a_repository_is_healed_through_pre_instrumented_files(toy, tmp_path):
    repo, rules = toy
    cache = tmp_path / "cache"
    written, failed = pi.instrument_tree_of(str(repo), str(repo), str(cache), "4")
    assert written == 2 and not failed                    # __init__.py and core.py; tests are left alone
    p = run_toy(repo, rules, tmp_path, {"TXH_PRE_AST": "1"})
    assert "2 passed" in p.stdout, p.stdout + p.stderr
    log = [json.loads(x) for x in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert sum(e["status"] == "COMMITTED" for e in log) == 2


@pytest.mark.skipif(not CAN_BUILD, reason="instrumenting needs Python 3.9+")
def test_a_file_without_an_entry_is_loaded_unchanged(toy, tmp_path):
    repo, rules = toy
    p = run_toy(repo, rules, tmp_path, {"TXH_PRE_AST": "1"})      # empty cache: nothing instrumented
    assert "2 failed" in p.stdout, p.stdout + p.stderr           # the repository code runs unhealed
    assert "2 left unchanged" in p.stdout                         # both modules were skipped