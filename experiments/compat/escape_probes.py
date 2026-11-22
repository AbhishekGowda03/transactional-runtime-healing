"""Which file, process and network operations are noticed by audit hooks and by the wrapper-based
replacement (compat_hooks.py, for Python 3.7)?

    uv run python experiments/compat/escape_probes.py              # compares the modes below
    python experiments/compat/escape_probes.py --child             # (internal) one mode, JSON out

Modes: audit (sys.addaudithook, Python 3.8+), shim-no-sweep (wrappers only) and shim (wrappers plus
rebinding of references taken before install). On Python 3.7 only the two shim modes can run.
Every probe performs one operation in a temporary directory. What "noticed" means depends on the kind:
  fs      the operation really changes the directory, and the file-system journal (the part of the
          protocol that undoes a rejected trial) restores it exactly. An event of some other kind
          (Path.touch also reports os.utime) does not count: the creation of the file has to be seen.
  event   the operation is a process, network or library effect, and the protocol records a blocked
          effect for it (watch in record-only mode).
  meta    a metadata change that cannot be seen in the directory (chown to the same owner): the
          protocol must record an fs event with the expected name.
Connections time out after 1 second (on WSL a connect to a closed port can hang).
Imports that real programs do before the healer starts ("from os import remove", pathlib, ...)
are done first on purpose: they are the references that wrappers can miss.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile


def child():
    import io, pathlib, shutil, socket, subprocess as sp, tempfile as tf, ctypes   # noqa
    import _io, _socket
    try:
        import posix
    except ImportError:
        posix = os
    d = tempfile.mkdtemp()
    f = lambda n: os.path.join(d, n)
    with open(f("early_mod.py"), "w") as fh:         # a module that took references early, as real code does
        fh.write("from os import remove as early_remove\nfrom socket import getaddrinfo as early_getaddrinfo\n"
                 "from shutil import rmtree as early_rmtree\n")
    sys.path.insert(0, d)
    import early_mod
    from transactional_runtime_healing import protocol as P
    P.install_hook()

    def touch(n):
        with open(f(n), "w") as fh:            # outside the watch: setup is not a probe
            fh.write("x")
        return f(n)

    socket.setdefaulttimeout(1)
    probes = {}

    def probe(name, setup=None, kind="fs", expect=None):
        def deco(op):
            probes[name] = (kind, setup, op, expect)
            return op
        return deco

    def snapshot():
        """Everything in the test directory: type, mode, content or link target."""
        snap = {}
        for root, dirs, files in os.walk(d):
            for n in dirs + files:
                full = os.path.join(root, n)
                st = os.lstat(full)
                rel = os.path.relpath(full, d)
                if stat.S_ISLNK(st.st_mode):
                    snap[rel] = ("link", os.readlink(full))
                elif stat.S_ISDIR(st.st_mode):
                    snap[rel] = ("dir", stat.S_IMODE(st.st_mode))
                else:
                    with open(full, "rb") as fh:
                        snap[rel] = ("file", stat.S_IMODE(st.st_mode), fh.read())
        return snap

    fds = {}

    def openfd(n):                                # a descriptor opened outside the watch
        def setup():
            touch(n)
            fds[n] = os.open(f(n), os.O_RDWR)
        return setup

    probe("open(w)")(lambda: open(f("a"), "w").close())
    probe("io.open(w)")(lambda: io.open(f("b"), "w").close())
    probe("os.open(O_WRONLY|O_CREAT)")(lambda: os.close(os.open(f("c"), os.O_WRONLY | os.O_CREAT)))
    probe("pathlib.Path.write_text")(lambda: pathlib.Path(f("d")).write_text("x"))
    probe("pathlib.Path.touch (new file)")(lambda: pathlib.Path(f("d2")).touch())
    probe("pathlib.Path.unlink", lambda: touch("e"))(lambda: pathlib.Path(f("e")).unlink())
    probe("pathlib.Path.mkdir")(lambda: pathlib.Path(f("dir1")).mkdir())
    probe("pathlib.Path.rename", lambda: touch("g"))(lambda: pathlib.Path(f("g")).rename(f("g2")))
    probe("os.remove", lambda: touch("h"))(lambda: os.remove(f("h")))
    probe("os.unlink", lambda: touch("i"))(lambda: os.unlink(f("i")))
    probe("os.rename", lambda: touch("j"))(lambda: os.rename(f("j"), f("j2")))
    probe("os.makedirs")(lambda: os.makedirs(f("m/n")))
    probe("os.chmod", lambda: touch("c1"))(lambda: os.chmod(f("c1"), 0o600))
    probe("os.fchmod", openfd("c2"))(lambda: os.fchmod(fds["c2"], 0o600))
    probe("os.ftruncate", openfd("c3"))(lambda: os.ftruncate(fds["c3"], 0))
    probe("os.truncate", lambda: touch("c4"))(lambda: os.truncate(f("c4"), 0))
    probe("os.utime", lambda: touch("c5"), "meta", "os.utime")(lambda: os.utime(f("c5"), (1, 1)))
    probe("os.symlink")(lambda: os.symlink("a", f("c6")))
    probe("os.link", lambda: touch("c7"))(lambda: os.link(f("c7"), f("c8")))
    probe("os.lchown (same owner)", lambda: touch("c9"), "meta", "os.chown")(
        lambda: os.lchown(f("c9"), -1, -1))
    probe("os.fchown (same owner)", openfd("c10"), "meta", "os.chown")(
        lambda: os.fchown(fds["c10"], -1, -1))
    probe("shutil.rmtree", lambda: os.makedirs(f("t/u")))(lambda: shutil.rmtree(f("t")))
    probe("shutil.move", lambda: touch("k"))(lambda: shutil.move(f("k"), f("k2")))
    probe("shutil.copy", lambda: touch("l"))(lambda: shutil.copy(f("l"), f("l2")))
    probe("tempfile.NamedTemporaryFile")(lambda: tf.NamedTemporaryFile(dir=d, delete=False).close())
    probe("early 'from os import remove'", lambda: touch("p"))(lambda: early_mod.early_remove(f("p")))
    probe("early 'from shutil import rmtree'", lambda: os.makedirs(f("q")))(lambda: early_mod.early_rmtree(f("q")))
    probe("posix.remove", lambda: touch("r"))(lambda: posix.remove(f("r")))
    probe("_io.open(w)")(lambda: _io.open(f("s"), "w").close())
    probe("io.FileIO(w)")(lambda: io.FileIO(f("t2"), "w").close())
    probe("subprocess.run", kind="event")(lambda: sp.run(["true"]))
    probe("os.system", kind="event")(lambda: os.system("true"))
    probe("os.kill", kind="event")(lambda: os.kill(os.getpid(), 0))
    probe("socket.connect", kind="event")(lambda: socket.socket().connect(("127.0.0.1", 1)))
    probe("_socket.socket.connect", kind="event")(lambda: _socket.socket().connect(("127.0.0.1", 1)))
    probe("socket.getaddrinfo", kind="event")(lambda: socket.getaddrinfo("localhost", 80))
    probe("early 'from socket import getaddrinfo'", kind="event")(
        lambda: early_mod.early_getaddrinfo("localhost", 80))
    probe("ctypes.CDLL(None)", kind="event")(lambda: ctypes.CDLL(None))

    out = {}
    jbase = tempfile.mkdtemp()                    # the journal lives outside the test directory
    for name, (kind, setup, op, expect) in probes.items():
        if setup:
            setup()
        if kind == "event":
            P.begin_watch(record_only=True)
            try:
                op()
            except Exception:
                pass
            effects, fs = P.end_watch()
            out[name] = bool(effects)
        elif kind == "meta":
            P.begin_watch(record_only=True)
            try:
                op()
            except Exception:
                pass
            effects, fs = P.end_watch()
            out[name] = any(e.startswith(expect) for e in fs)
        else:
            from transactional_runtime_healing.fs_journal import FsJournal
            before = snapshot()
            j = FsJournal.create(jbase)
            P.begin_watch(journal=j)
            try:
                op()
            except Exception:
                pass
            P.end_watch()
            changed = snapshot() != before
            j.undo()
            restored = snapshot() == before
            j.discard()
            if not changed:                            # the probe does not change anything: a bug here
                print("probe changed nothing:", name, file=sys.stderr)
            out[name] = changed and restored
        for fd in list(fds.values()):
            try:
                os.close(fd)
            except OSError:
                pass
        fds.clear()
    print(json.dumps(out))


def run_mode(label, env_extra):
    env = dict(os.environ, **env_extra)
    here = os.path.dirname(os.path.abspath(__file__))
    env["PYTHONPATH"] = os.path.join(here, "..", "..", "src") + os.pathsep + env.get("PYTHONPATH", "")
    try:
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--child"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, env=env, timeout=120)
    except subprocess.TimeoutExpired:
        print(label, "failed: timeout after 120 s")
        return None
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        print(label, "failed:", p.stderr[-500:])
        return None


def main():
    modes = []
    if hasattr(sys, "addaudithook"):
        modes.append(("audit hook", {"TXH_HOOKS": ""}))
    modes += [("wrappers", {"TXH_HOOKS": "shim", "TXH_SHIM_SWEEP": "0"}),
              ("wrappers + rebind", {"TXH_HOOKS": "shim", "TXH_SHIM_SWEEP": "1"})]
    results = [(m, run_mode(m, e)) for m, e in modes]
    results = [(m, r) for m, r in results if r]
    names = list(results[0][1])
    w = max(len(n) for n in names) + 2
    print("Python", sys.version.split()[0])
    print("".ljust(w) + "".join(m.ljust(20) for m, _ in results))
    for n in names:
        print(n.ljust(w) + "".join(("noticed" if r[n] else "MISSED").ljust(20) for _, r in results))
    print("".ljust(w) + "".join(f"{sum(r.values())}/{len(r)}".ljust(20) for _, r in results))


if __name__ == "__main__":
    child() if "--child" in sys.argv else main()