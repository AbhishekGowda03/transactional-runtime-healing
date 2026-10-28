"""Transactional healing protocol (version 4, fork only).

Steps: Begin (fork) -> Trial (run repair in child) -> Validate (checks)
       -> Commit or Restore.

This module holds (a) `transactional_heal`, where the child continues the
program, and (b) the trial machinery shared with `heal_point` (trial in a child,
commit by replay in the live process).

Design decisions (to be stated in the methodology chapter):

* Commit in `transactional_heal` = the child continues the program (`resume`)
  and reports the result to the parent through a pipe; the parent returns it.
  The parent never receives the child's heap, so the result must be picklable.
  Passing the whole healed state back was rejected (not all objects can be
  pickled). The repair never modifies the original process, except through
  inherited sockets and pipes (see Limits). `heal_point` uses a different commit
  (replay in the live process) and is the design intended for HealBench.
* The validation point is BEFORE `resume`. Whether the repair is accepted and
  what happens afterwards in the continuation are separate outcomes.
* External effects are detected with a Python audit hook (sys.addaudithook),
  which is recorded in a flag. A repair that catches the exception and goes on
  is still rejected. Filesystem writes are only RECORDED (not blocked); a
  copy-on-write filesystem layer is the planned second step.
* The audit hook is process-wide and controlled by a flag. While the flag is
  on (in the child during a trial, and in the live process during a replay) it
  applies to EVERY thread of that process, not only to the repair code.
  Threads are out of scope.
* Output of the trial is held back at file-descriptor level (fd 1 and 2), so
  print, logging and os.write are all held back. It is replayed only on commit.
  Python's sys.stdout/sys.stderr are re-pointed at fd 1/2 during the trial. A
  repair that installs its own sys.stdout object (contextlib.redirect_stdout) is
  not covered, which is usually harmless: nothing reaches the terminal.
* Inherited file descriptors are shared with the parent after fork (same open
  file description). Offsets of inherited REGULAR files are snapshotted before
  and compared after the trial; a change is rejected and the offset restored.
  Sockets/pipes have no offset and no audit event for send/write: known gap.
* pickle is used between the parent and its own forked child, so the data is
  trusted.

Timeouts: `timeout` covers trial + validation only. After the verdict the
continuation has its own limit (`continuation_timeout`, None = unlimited).

Limits: send/write on inherited sockets and pipes; native code (ctypes calls,
C extensions with their own syscalls) bypasses audit events; threads/locks
held at fork time can hang the child (timeout turns this into a rejection).
"""
import io
import os
import pickle
import select
import signal
import stat
import struct
import sys
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, List, Optional, Tuple


class ExternalEffectBlocked(Exception):
    """Raised inside the trial when the repair attempts an external effect."""


class Status(Enum):
    COMMITTED = "committed"
    REJECTED_REPAIR_ERROR = "rejected: repair raised an exception"
    REJECTED_CHECK = "rejected: a check failed"
    REJECTED_EXTERNAL_EFFECT = "rejected: external effect attempted"
    REJECTED_INHERITED_FD = "rejected: inherited file offset changed"
    REJECTED_TIMEOUT = "rejected: timeout during trial"
    REJECTED_CHILD_DIED = "rejected: child died during trial"


@dataclass
class Outcome:
    status: Status
    reason: str
    # Only meaningful when committed: what happened in the continuation.
    #   ok | original_error_again | new_error | timeout | child_died
    #   | result_not_transferable
    continuation: Optional[str] = None
    result: Any = None
    error: Optional[str] = None
    effects: List[str] = field(default_factory=list)      # blocked external effects
    fs_effects: List[str] = field(default_factory=list)   # recorded filesystem writes
    fd_effects: List[str] = field(default_factory=list)   # inherited fd offsets changed

    @property
    def committed(self) -> bool:
        return self.status is Status.COMMITTED


# ----------------------------------------------------------------------
# Effect detection (runs only inside the forked child, or during a replay)
# ----------------------------------------------------------------------
_BLOCKED_EVENTS = {
    "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
    "os.fork", "os.forkpty",
    "socket.connect", "socket.sendto", "socket.sendmsg",
    "socket.getaddrinfo", "socket.gethostbyname",
    "ctypes.dlopen", "ctypes.call_function",
    "os.kill", "os.killpg", "signal.pthread_kill",
}
_FS_EVENTS = {"os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.truncate",
              "shutil.rmtree", "shutil.move", "os.chmod", "os.chown", "os.utime",
              "os.link", "os.symlink"}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

_trial = {"active": False, "effects": [], "fs": [], "allow": frozenset(), "on_effect": None}
_hook_installed = False


def _audit_hook(event, args):
    if not _trial["active"]:
        return
    if event in _BLOCKED_EVENTS and event not in _trial["allow"]:
        _trial["effects"].append(event)          # flag: cannot be swallowed
        if _trial["on_effect"] is not None:
            _trial["on_effect"](event)           # e.g. reject the trial at once
        raise ExternalEffectBlocked(event)       # and stop the effect
    if event == "open":
        path, mode, flags = args
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                 (isinstance(flags, int) and flags & _WRITE_FLAGS)
        if writes and "__pycache__" not in str(path):
            _trial["fs"].append(f"open-write:{path}")
    elif event in _FS_EVENTS:
        _trial["fs"].append(f"{event}:{args[0] if args else ''}")


def install_hook():
    global _hook_installed
    if not _hook_installed:
        sys.addaudithook(_audit_hook)            # cannot be removed; flag controls it
        _hook_installed = True


def begin_watch(allow=frozenset(), on_effect=None):
    """Switch effect detection on (clears earlier records).

    allow     -- audit events that are NOT blocked (for example "ctypes.dlopen" when
                 the program imports libraries that use ctypes)
    on_effect -- called with the event name when an external effect is attempted,
                 before the exception is raised (the live trial uses it to reject at once)
    """
    install_hook()
    _trial.update(active=True, effects=[], fs=[], allow=frozenset(allow), on_effect=on_effect)


def end_watch() -> Tuple[List[str], List[str]]:
    """Switch effect detection off; return (blocked effects, recorded fs writes)."""
    _trial["active"] = False
    _trial["on_effect"] = None
    return list(_trial["effects"]), list(_trial["fs"])


def fd_offsets():
    """Offsets of inherited regular files (shared with the parent after fork)."""
    pos = {}
    for name in os.listdir("/proc/self/fd"):
        try:
            fd = int(name)
            if stat.S_ISREG(os.fstat(fd).st_mode):
                pos[fd] = os.lseek(fd, 0, os.SEEK_CUR)
        except OSError:
            pass                                  # e.g. the listdir fd itself
    return pos


def error_site(e):
    """(full file path, line) where the exception was raised (innermost frame)."""
    tb = e.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    return None if tb is None else (tb.tb_frame.f_code.co_filename, tb.tb_lineno)


def site_matches(site, expected) -> bool:
    """expected = (path or basename, line); compares the full path when given."""
    if site is None or expected is None:
        return False
    path, line = site
    exp_path, exp_line = expected
    return line == exp_line and (path == exp_path or os.path.basename(path) == exp_path)


# ----------------------------------------------------------------------
# Output hold-back at file-descriptor level
# ----------------------------------------------------------------------
class _FdCapture:
    """Send fd 1 and 2 to temp files; keep the bytes for replay on commit.

    Use as a context manager, or call start()/stop() when the capture has to
    last across calls (the live trial). After stop(), `out` and `err` hold the
    captured bytes."""

    def start(self):
        sys.stdout.flush()
        sys.stderr.flush()
        self._files = [tempfile.TemporaryFile(), tempfile.TemporaryFile()]
        self._saved = [os.dup(1), os.dup(2)]
        os.dup2(self._files[0].fileno(), 1)
        os.dup2(self._files[1].fileno(), 2)
        # Point Python's own streams at fd 1/2 too: sys.stdout may be some other
        # object (pytest capture, a wrapper) that does not write to fd 1.
        self._streams = (sys.stdout, sys.stderr)
        sys.stdout = io.TextIOWrapper(io.FileIO(1, "w", closefd=False),
                                      encoding="utf-8", errors="replace", write_through=True)
        sys.stderr = io.TextIOWrapper(io.FileIO(2, "w", closefd=False),
                                      encoding="utf-8", errors="replace", write_through=True)
        self.out = self.err = b""
        return self

    def stop(self):
        sys.stdout.flush()
        sys.stderr.flush()
        sys.stdout, sys.stderr = self._streams
        os.dup2(self._saved[0], 1)
        os.dup2(self._saved[1], 2)
        for fd in self._saved:
            os.close(fd)
        for f, name in zip(self._files, ("out", "err")):
            f.seek(0)
            setattr(self, name, f.read())
            f.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


def write_all(fd, data):
    while data:
        n = os.write(fd, data)
        data = data[n:]


# ----------------------------------------------------------------------
# The trial, shared by transactional_heal and heal_point (runs in the child)
# ----------------------------------------------------------------------
@dataclass
class TrialReport:
    verdict: Optional[str]            # None = accepted | error | check | effect | fd
    reason: str = ""
    effects: List[str] = field(default_factory=list)
    fs: List[str] = field(default_factory=list)
    fd: List[str] = field(default_factory=list)
    out: bytes = b""
    err: bytes = b""
    value: Any = None


def trial_in_child(attempt: Callable[[], Any],
                   check: Callable[[Any], Optional[str]]) -> TrialReport:
    """attempt() runs the repair and returns a value; check(value) returns None if
    all checks pass, else the failure message. Output is held back (fd level),
    effects are flagged, inherited file offsets are compared and restored."""
    fd_before = fd_offsets()
    verdict = reason = value = None
    with _FdCapture() as cap:
        begin_watch()
        try:
            try:
                value = attempt()
            except BaseException as e:
                if not _trial["effects"]:
                    verdict, reason = "error", f"repair raised {e!r}"
            if verdict is None:
                try:
                    msg = check(value)
                except BaseException as e:
                    msg = f"check raised {e!r}"
                if msg is not None:
                    verdict, reason = "check", msg
        finally:
            effects, fs = end_watch()
    fd_after = fd_offsets()
    moved = []
    for fd, off in fd_before.items():
        if fd_after.get(fd, off) != off:
            moved.append(f"fd{fd}:{off}->{fd_after[fd]}")
            os.lseek(fd, off, os.SEEK_SET)        # undo the shared offset change
    if effects:                                   # even if swallowed
        verdict = "effect"
        reason = "external effect attempted: " + ", ".join(sorted(set(effects)))
    elif moved:
        verdict = "fd"
        reason = "inherited file offset changed: " + ", ".join(moved)
    return TrialReport(verdict, reason, effects, fs, moved, cap.out, cap.err, value)


# ----------------------------------------------------------------------
# Child -> parent messages: length-prefixed pickles
# ----------------------------------------------------------------------
def send(w, kind, payload):
    data = pickle.dumps((kind, payload))
    write_all(w, struct.pack(">I", len(data)) + data)


def _parse(buf):
    msgs = []
    while len(buf) >= 4:
        n = struct.unpack(">I", buf[:4])[0]
        if len(buf) < 4 + n:
            break
        msgs.append(pickle.loads(buf[4:4 + n]))
        buf = buf[4 + n:]
    return msgs, buf


def collect(r, pid, timeout, trigger=None, after_timeout=None):
    """Read messages from the child until it closes the pipe or time runs out.

    After a message of kind `trigger` arrives, the deadline becomes
    `after_timeout` seconds from then (None = no limit). The child is killed on a
    timeout and on any exception (for example Ctrl-C), so it is never orphaned.
    Returns (messages, timed_out)."""
    buf, msgs = b"", []
    deadline = time.monotonic() + timeout
    timed_out = finished = triggered = False
    try:
        while True:
            if deadline is None:
                left = None
            else:
                left = deadline - time.monotonic()
                if left <= 0:
                    timed_out = True
                    break
            ready, _, _ = select.select([r], [], [], left)
            if not ready:
                continue
            chunk = os.read(r, 65536)
            if not chunk:
                break
            new, buf = _parse(buf + chunk)
            msgs += new
            if trigger and not triggered and any(k == trigger for k, _ in msgs):
                triggered = True
                deadline = (None if after_timeout is None
                            else time.monotonic() + after_timeout)
        finished = True
    finally:
        if not finished or timed_out:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        os.close(r)
        os.waitpid(pid, 0)
    return msgs, timed_out


_VERDICT_STATUS = {"error": Status.REJECTED_REPAIR_ERROR, "check": Status.REJECTED_CHECK,
                   "effect": Status.REJECTED_EXTERNAL_EFFECT,
                   "fd": Status.REJECTED_INHERITED_FD}


def _child(w, state, repair, checks, resume, original_error, original_site):
    install_hook()

    def attempt():
        repair(state)

    def check(_):
        for c in checks:
            try:
                ok, msg = c(state)
            except BaseException as e:
                ok, msg = False, f"check raised {e!r}"
            if not ok:
                return msg
        return None

    rep = trial_in_child(attempt, check)
    if rep.verdict is not None:                               # Restore side
        send(w, "rejected", dict(verdict=rep.verdict, reason=rep.reason,
                                 effects=rep.effects, fs=rep.fs, fd=rep.fd))
        os._exit(0)                                           # held-back output dropped

    # Commit: replay held-back output, tell the parent, then continue.
    write_all(1, rep.out)
    write_all(2, rep.err)
    send(w, "validated", dict(effects=rep.effects, fs=rep.fs, fd=rep.fd))
    try:
        result = resume(state)
        try:
            pickle.dumps(result)
            payload = dict(continuation="ok", result=result)
        except Exception as e:
            payload = dict(continuation="result_not_transferable", error=repr(e))
    except BaseException as e:
        again = original_error is not None and isinstance(e, original_error) and (
            original_site is None or site_matches(error_site(e), original_site))
        payload = dict(continuation="original_error_again" if again else "new_error",
                       error=repr(e))
    send(w, "continued", payload)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def transactional_heal(state, repair: Callable, checks: list, resume: Callable,
                       timeout: float = 10.0,
                       original_error: Optional[type] = None,
                       original_site: Optional[tuple] = None,
                       continuation_timeout: Optional[float] = None) -> Outcome:
    """Try `repair(state)` in an isolated child; commit only if all checks pass.

    state          -- program state at the crash point (mutable object)
    repair         -- repair code (LLM-generated in the real system)
    checks         -- list of check(state) -> (ok: bool, message: str)
    resume         -- continues the program after the crash point; returns result
    timeout        -- seconds for trial + validation (until the verdict)
    original_error -- exception type of the original crash, to recognise it again
    original_site  -- optional (path or basename, line) of the original crash (use
                      `error_site(exception)` to get it); if given, "again" needs the
                      same type AND the same raise location
    continuation_timeout -- seconds for the continuation after commit (None = none)
    """
    sys.stdout.flush()            # avoid duplicating buffered output in the child
    sys.stderr.flush()
    r, w = os.pipe()
    pid = os.fork()                                           # Begin
    if pid == 0:
        os.close(r)
        _child(w, state, repair, checks, resume, original_error, original_site)
    os.close(w)
    msgs, timed_out = collect(r, pid, timeout, "validated", continuation_timeout)

    got = {kind: payload for kind, payload in msgs}
    if "rejected" in got:
        p = got["rejected"]
        return Outcome(_VERDICT_STATUS[p["verdict"]], p["reason"], effects=p["effects"],
                       fs_effects=p["fs"], fd_effects=p["fd"])
    if "validated" not in got:
        if timed_out:
            return Outcome(Status.REJECTED_TIMEOUT, f"no verdict within {timeout}s")
        return Outcome(Status.REJECTED_CHILD_DIED, "child ended without a verdict")
    v = got["validated"]
    out = Outcome(Status.COMMITTED, "all checks passed", effects=v["effects"],
                  fs_effects=v["fs"], fd_effects=v["fd"])
    if "continued" in got:
        c = got["continued"]
        out.continuation, out.result, out.error = (
            c["continuation"], c.get("result"), c.get("error"))
    else:
        out.continuation = "timeout" if timed_out else "child_died"
    return out


def plain_heal(state, repair: Callable, resume: Callable) -> Any:
    """Baseline: run the repair directly in the live process (no protection)."""
    repair(state)
    return resume(state)