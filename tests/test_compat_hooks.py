"""compat_hooks: the wrapper-based replacement for audit hooks (Python 3.7).

Each test runs in a fresh interpreter with TXH_HOOKS=shim, because the wrappers cannot be removed.
"""
import os
import subprocess
import sys
import textwrap

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def run(code, tmp_path):
    env = dict(os.environ, TXH_HOOKS="shim", PYTHONPATH=SRC)
    p = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True,
                       env=env, cwd=tmp_path, timeout=60)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def test_wrappers_report_file_and_process_events(tmp_path):
    out = run("""
        import os
        from transactional_runtime_healing import protocol as P
        P.install_hook()
        P.begin_watch(record_only=True)
        open("a.txt", "w").close()
        os.rename("a.txt", "b.txt")
        os.remove("b.txt")
        os.system("true")
        effects, fs = P.end_watch()
        print(sorted(set(effects)), sorted(set(e.split(":")[0] for e in fs)))
    """, tmp_path)
    assert out == "['os.system'] ['open-write', 'os.remove', 'os.rename']"


def test_effects_are_blocked_and_the_original_is_not_called(tmp_path):
    out = run("""
        import os, socket
        from transactional_runtime_healing import protocol as P
        P.install_hook()
        P.begin_watch()
        r = []
        try:
            socket.socket().connect(("127.0.0.1", 9))
        except P.ExternalEffectBlocked as e:
            r.append("connect blocked")
        try:
            os.system("echo should-not-run > ran.txt")
        except P.ExternalEffectBlocked:
            r.append("system blocked")
        P.end_watch()
        print(r, os.path.exists("ran.txt"))
    """, tmp_path)
    assert out == "['connect blocked', 'system blocked'] False"


def test_calls_outside_a_watch_and_errors_pass_through_unchanged(tmp_path):
    out = run("""
        import os
        from transactional_runtime_healing import protocol as P
        P.install_hook()
        open("x", "w").write("hi")
        print(open("x").read(), os.path.exists("x"))
        try:
            os.remove("missing")
        except FileNotFoundError as e:
            print("FileNotFoundError", e.filename)
        try:
            os.remove()
        except TypeError:
            print("TypeError")
    """, tmp_path)
    assert out.splitlines() == ["hi True", "FileNotFoundError missing", "TypeError"]


def test_references_taken_before_install_are_rebound(tmp_path):
    (tmp_path / "early.py").write_text("from os import remove as early_remove\n")
    out = run("""
        import sys
        sys.path.insert(0, ".")
        import early
        from transactional_runtime_healing import protocol as P
        P.install_hook()
        open("f", "w").close()
        P.begin_watch(record_only=True)
        early.early_remove("f")
        effects, fs = P.end_watch()
        print(fs)
    """, tmp_path)
    assert out == "['os.remove:f']"


def test_the_journal_undoes_a_file_the_wrappers_saw(tmp_path):
    out = run("""
        import os
        from transactional_runtime_healing import protocol as P
        from transactional_runtime_healing.fs_journal import FsJournal
        P.install_hook()
        open("keep.txt", "w").write("original")
        j = FsJournal.create(os.getcwd())
        P.begin_watch(journal=j)
        open("keep.txt", "w").write("changed")
        os.remove("keep.txt")
        P.end_watch()
        j.undo()
        print(open("keep.txt").read())
    """, tmp_path)
    assert out == "original"


def test_path_touch_creating_a_file_is_undone(tmp_path):
    # Path.touch goes through pathlib's accessor `open` (os.open with flags on 3.7 to 3.9); the
    # event must show a write, or the journal does not remove the file a rejected trial created
    out = run("""
        import os, pathlib
        from transactional_runtime_healing import protocol as P
        from transactional_runtime_healing.fs_journal import FsJournal
        P.install_hook()
        j = FsJournal.create(os.getcwd())
        P.begin_watch(journal=j)
        pathlib.Path("made.txt").touch()
        P.end_watch()
        existed = os.path.exists("made.txt")
        j.undo()
        print(existed, os.path.exists("made.txt"))
    """, tmp_path)
    assert out == "True False"


def test_descriptor_based_metadata_calls_are_reported(tmp_path):
    out = run("""
        import os
        from transactional_runtime_healing import protocol as P
        P.install_hook()
        open("f", "w").close()
        fd = os.open("f", os.O_RDWR)
        P.begin_watch(record_only=True)
        os.fchmod(fd, 0o600)
        os.fchown(fd, -1, -1)
        os.ftruncate(fd, 0)
        os.lchown("f", -1, -1)
        effects, fs = P.end_watch()
        print(sorted(set(e.split(":")[0] for e in fs)))
    """, tmp_path)
    assert out == "['os.chmod', 'os.chown', 'os.truncate']"


def test_fchmod_is_undone(tmp_path):
    out = run("""
        import os, stat
        from transactional_runtime_healing import protocol as P
        from transactional_runtime_healing.fs_journal import FsJournal
        P.install_hook()
        open("f", "w").close()
        os.chmod("f", 0o644)
        fd = os.open("f", os.O_RDWR)
        j = FsJournal.create(os.getcwd())
        P.begin_watch(journal=j)
        os.fchmod(fd, 0o600)
        P.end_watch()
        j.undo()
        print(oct(stat.S_IMODE(os.stat("f").st_mode)))
    """, tmp_path)
    assert out == "0o644"


def test_popen_event_has_executable_cwd_and_the_command_as_run(tmp_path):
    out = run("""
        import subprocess
        from transactional_runtime_healing import compat_hooks, protocol as P
        seen = []
        compat_hooks.install(lambda event, args: seen.append((event, args)) if event == "subprocess.Popen" else None)
        subprocess.run(["true"], cwd="/tmp")
        subprocess.run("true", shell=True, cwd="/tmp")
        subprocess.Popen(["true"], -1, None, None, None, None, None, True, False, "/tmp").wait()
        print(seen)
    """, tmp_path)
    assert out == ("[('subprocess.Popen', ('true', ['true'], '/tmp', None)), "
                   "('subprocess.Popen', ('/bin/sh', ['/bin/sh', '-c', 'true'], '/tmp', None)), "
                   "('subprocess.Popen', ('true', ['true'], '/tmp', None))]")