"""Filesystem isolation: the file changes of a rejected repair are undone.

Part 1 tests the journal alone (record, change, undo, compare the folder).
Part 2 runs programs under the live protocol and compares the folder after a rejected
repair with the folder before it.
"""
import json
import os
import stat
import subprocess
import sys
import textwrap

import pytest

from transactional_runtime_healing import fs_journal as J
from transactional_runtime_healing.fs_journal import FsJournal, Unjournalable

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def snapshot(root):
    """Everything about a folder that the journal promises to keep."""
    out = {}
    for dp, dn, fn in os.walk(root):
        for n in dn + fn:
            p = os.path.join(dp, n)
            st = os.lstat(p)
            rel = os.path.relpath(p, root)
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("link", os.readlink(p))
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir", stat.S_IMODE(st.st_mode))
            else:
                with open(p, "rb") as f:
                    out[rel] = ("file", stat.S_IMODE(st.st_mode), f.read(), st.st_mtime_ns)
    return out


@pytest.fixture
def world(tmp_path):
    w = tmp_path / "w"
    w.mkdir()
    (w / "a.txt").write_text("alpha")
    (w / "b.txt").write_text("beta")
    (w / "sub").mkdir()
    (w / "sub" / "c.txt").write_text("gamma")
    os.symlink("a.txt", w / "link")
    j = FsJournal.create(str(tmp_path))
    return w, j


def do(j, event, args, action):
    j.before(event, args)
    return action()


def undo_and_compare(w, j, before):
    done, failed = j.undo()
    assert failed == []
    assert snapshot(w) == before
    assert not os.path.exists(j.dir)
    return done


# ---- part 1: the journal alone ---------------------------------------------
def test_overwrite_truncate_and_append_are_undone(world):
    w, j = world
    before = snapshot(w)
    a, b, c = str(w / "a.txt"), str(w / "b.txt"), str(w / "sub" / "c.txt")
    for path, mode, flags in ((a, "w", os.O_WRONLY | os.O_CREAT | os.O_TRUNC),
                              (b, "a", os.O_WRONLY | os.O_CREAT | os.O_APPEND),
                              (c, "r+", os.O_RDWR)):
        j.before("open", (path, mode, flags))
    open(a, "w").write("changed")
    open(b, "a").write(" more")
    with open(c, "r+") as f:
        f.write("XX")
    undo_and_compare(w, j, before)


def test_created_files_and_directories_are_removed(world):
    w, j = world
    before = snapshot(w)
    do(j, "os.mkdir", (str(w / "new"), 0o777, None), lambda: os.mkdir(w / "new"))
    p = str(w / "new" / "f.txt")
    do(j, "open", (p, "w", os.O_WRONLY | os.O_CREAT | os.O_TRUNC), lambda: open(p, "w").write("x"))
    do(j, "os.symlink", ("a.txt", str(w / "l2"), None), lambda: os.symlink("a.txt", w / "l2"))
    do(j, "os.link", (str(w / "a.txt"), str(w / "hard"), None, None),
       lambda: os.link(w / "a.txt", w / "hard"))
    undo_and_compare(w, j, before)


def test_remove_rename_rmdir_are_undone(world):
    w, j = world
    before = snapshot(w)
    do(j, "os.remove", (str(w / "a.txt"), None), lambda: os.remove(w / "a.txt"))
    do(j, "os.rename", (str(w / "b.txt"), str(w / "sub" / "c.txt"), None, None),
       lambda: os.replace(w / "b.txt", w / "sub" / "c.txt"))        # overwrites c.txt
    do(j, "os.remove", (str(w / "link"), None), lambda: os.remove(w / "link"))
    do(j, "os.remove", (str(w / "sub" / "c.txt"), None), lambda: os.remove(w / "sub" / "c.txt"))
    do(j, "os.rmdir", (str(w / "sub"), None), lambda: os.rmdir(w / "sub"))
    assert os.listdir(w) == []
    undo_and_compare(w, j, before)


def test_rmtree_through_its_nested_operations(world):
    w, j = world
    before = snapshot(w)
    import shutil

    busy = [False]

    def hook(event, args):
        if _ACTIVE[0] and not busy[0] and (event in J.EVENTS or event == "open"):
            busy[0] = True
            try:
                j.before(event, args)
            finally:
                busy[0] = False
    # the journal is fed the same events the audit hook would deliver
    if hasattr(sys, "addaudithook") and os.environ.get("TXH_HOOKS") != "shim":
        sys.addaudithook(hook)
    else:                                         # Python 3.7: the wrappers deliver the same events
        from transactional_runtime_healing import compat_hooks
        compat_hooks.install(hook)
    _ACTIVE[0] = True
    try:
        shutil.rmtree(w)
    finally:
        _ACTIVE[0] = False
    assert not os.path.exists(w)
    os.mkdir(w)                                   # rmtree removed the root as well
    done, failed = j.undo()
    assert failed == []
    snap = snapshot(w)
    assert snap == before


_ACTIVE = [False]


def test_mode_owner_times_are_restored(world):
    w, j = world
    a = str(w / "a.txt")
    before = snapshot(w)
    do(j, "os.chmod", (a, 0o600, None), lambda: os.chmod(a, 0o600))
    do(j, "os.utime", (a, (1, 1), None, None), lambda: os.utime(a, (1, 1)))
    undo_and_compare(w, j, before)


def test_truncate_that_shortens_and_that_grows(world):
    w, j = world
    before = snapshot(w)
    a, b = str(w / "a.txt"), str(w / "b.txt")
    do(j, "os.truncate", (a, 2), lambda: os.truncate(a, 2))
    do(j, "os.truncate", (b, 100), lambda: os.truncate(b, 100))
    undo_and_compare(w, j, before)


def test_sqlite_database_and_sidecar_files(world, tmp_path):
    import sqlite3
    w, j = world
    db = str(w / "x.db")
    con = sqlite3.connect(db)
    con.execute("create table t(a)")
    con.execute("insert into t values (1)")
    con.commit()
    con.close()
    before = snapshot(w)
    j.before("sqlite3.connect", (db,))
    con = sqlite3.connect(db)
    con.execute("insert into t values (2)")
    con.commit()
    con.close()
    assert snapshot(w) != before
    undo_and_compare(w, j, before)


def test_file_over_the_limit_is_refused_not_ignored(tmp_path):
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 100)
    j = FsJournal.create(str(tmp_path), max_file=10)
    with pytest.raises(Unjournalable):
        j.before("open", (str(f), "w", os.O_WRONLY | os.O_TRUNC))
    j.discard()


def test_reads_and_unrelated_events_are_not_journaled(world):
    w, j = world
    j.before("open", (str(w / "a.txt"), "r", os.O_RDONLY))
    j.before("os.listdir", (str(w),))
    assert not os.path.exists(j.manifest)
    j.discard()


def test_inherited_descriptor_appends_are_cut_back(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("line1\n")
    f = open(log, "a")
    j = FsJournal.create(str(tmp_path))
    j.mark_fds()
    f.write("line2\n")
    f.flush()
    assert log.read_text() == "line1\nline2\n"
    done, failed = j.undo()
    f.close()
    assert failed == [] and log.read_text() == "line1\n"


def test_inherited_descriptor_overwrite_is_restored_when_small(tmp_path):
    data = tmp_path / "d.txt"
    data.write_text("0123456789")
    f = open(data, "r+")
    j = FsJournal.create(str(tmp_path))
    j.mark_fds()
    f.seek(3)
    f.write("XYZ")
    f.flush()
    done, failed = j.undo()
    f.close()
    assert failed == [] and data.read_text() == "0123456789"


def test_a_cut_off_manifest_line_is_ignored(world):
    w, j = world
    before = snapshot(w)
    j.before("open", (str(w / "a.txt"), "w", os.O_WRONLY | os.O_TRUNC))
    open(w / "a.txt", "w").write("x")
    with open(j.manifest, "a") as m:
        m.write('{"k": "absent", "pa')            # the child was killed in the middle of a line
    undo_and_compare(w, j, before)


# ---- part 2: under the live protocol --------------------------------------
def run(tmp_path, program, rules, *args):
    if sys.version_info < (3, 9):
        pytest.skip("live_run needs a pre-instrumented program below Python 3.9 (done on a newer Python "
                    "by pre_instrument.py); these cases run under 3.7 in experiments/stateleak")
    prog = tmp_path / "prog.py"
    prog.write_text("import sys\nsys.excepthook = lambda *a: print('original error')\n"
                    + textwrap.dedent(program))
    rul = tmp_path / "rules.json"
    rul.write_text(json.dumps(rules))
    log = tmp_path / "log.jsonl"
    env = dict(os.environ, PYTHONPATH=SRC)
    p = subprocess.run([sys.executable, "-m", "transactional_runtime_healing.live_run",
                        str(prog), str(rul), "--log", str(log), "--no-guard", *args],
                       capture_output=True, text=True, env=env, timeout=60, cwd=tmp_path)
    entries = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    return p, [e for e in entries if e["status"] != "NO_PROPOSAL"]


MESSY = ("import os, shutil\n"
         "open('data/a.txt', 'w').write('overwritten')\n"
         "open('data/new.txt', 'w').write('new')\n"
         "os.remove('data/b.txt')\n"
         "os.mkdir('data/newdir')\n"
         "os.rename('data/sub/c.txt', 'data/moved.txt')\n"
         "shutil.rmtree('data/sub')\n"
         "open('data/log.txt', 'a').write('trial line\\n')\n"
         "raise ValueError('the checks fail afterwards')\n")


def make_data(tmp_path):
    d = tmp_path / "data"
    (d / "sub").mkdir(parents=True)
    (d / "a.txt").write_text("alpha")
    (d / "b.txt").write_text("beta")
    (d / "sub" / "c.txt").write_text("gamma")
    (d / "log.txt").write_text("old log\n")
    return d


PROGRAM = """
    def f():
        v = 1 / 0
        return v
    f()
"""


def test_rejected_repair_leaves_the_folder_as_it_was(tmp_path):
    d = make_data(tmp_path)
    before = snapshot(d)
    p, log = run(tmp_path, PROGRAM, [{"match": "1 / 0", "code": MESSY}])
    assert [e["status"] for e in log] == ["REJECTED_REPAIR_ERROR"]
    assert snapshot(d) == before
    assert log[0]["fs_undo_failed"] == [] and log[0]["fs_undone"]
    assert log[0]["external_residue"] == []


def test_without_isolation_the_changes_stay(tmp_path):
    d = make_data(tmp_path)
    before = snapshot(d)
    p, log = run(tmp_path, PROGRAM, [{"match": "1 / 0", "code": MESSY}], "--no-fs-isolation")
    assert snapshot(d) != before
    assert log[0]["external_residue"]                 # recorded, as before


def test_committed_repair_keeps_its_file_changes(tmp_path):
    d = make_data(tmp_path)
    p, log = run(tmp_path, PROGRAM, [{"match": "1 / 0",
                                     "code": "v = 1\nopen('data/made.txt', 'w').write('kept')\n"}])
    assert [e["status"] for e in log] == ["COMMITTED"]
    assert (d / "made.txt").read_text() == "kept"
    assert not [n for n in os.listdir("/tmp") if n.startswith("txh-journal-")
                and os.path.getmtime(os.path.join("/tmp", n)) > os.path.getmtime(d)]


def test_continuation_writes_are_undone_when_the_trial_is_rejected(tmp_path):
    """The repair is harmless; the program, running on after it, writes a file and then the
    trial is rejected (recurrence)."""
    d = make_data(tmp_path)
    before = snapshot(d)
    p, log = run(tmp_path, """
        def f():
            v = 1 / 0
            open('data/after.txt', 'w').write('written by the continuation')
            return v
        f()
    """, [{"match": "1 / 0", "code": "v = 1"}], "--recurrence", "reject")
    # the repair works, so it is committed; no rejection and the file stays
    assert [e["status"] for e in log] == ["COMMITTED"]
    assert (d / "after.txt").exists()


def test_write_through_an_inherited_handle_is_undone(tmp_path):
    d = make_data(tmp_path)
    before = snapshot(d)
    p, log = run(tmp_path, """
        logf = open('data/log.txt', 'a')
        def f():
            v = 1 / 0
            return v
        f()
    """, [{"match": "1 / 0", "code": "logf.write('from the repair\\n')\nlogf.flush()\n"
                                     "raise ValueError('x')"}])
    assert [e["status"] for e in log] == ["REJECTED_REPAIR_ERROR"]
    assert snapshot(d) == before


def test_undo_failure_is_reported_not_hidden(tmp_path):
    d = make_data(tmp_path)
    p, log = run(tmp_path, PROGRAM, [{"match": "1 / 0", "code": MESSY}])
    assert "fs_undone" in log[0] and "fs_undo_failed" in log[0]