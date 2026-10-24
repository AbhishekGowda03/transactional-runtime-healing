"""Transactional healing protocol (version 3, fork only).

Steps: Begin (fork) -> Trial (run repair in child) -> Validate (checks)
       -> Commit (child continues) or Restore (child discarded).

Design decisions (to be stated in the methodology chapter):

* Commit = the child continues the program (`resume`) and reports the result
  to the parent through a pipe; the parent returns it. The parent never
  receives the child's heap, so the result must be picklable. Passing the
  whole healed state back to the parent was rejected (not all objects can be
  pickled). The original process is never modified by the repair.
* The validation point is BEFORE `resume`. Whether the repair is accepted and
  what happens afterwards in the continuation are separate outcomes.
* External effects are detected with a Python audit hook (sys.addaudithook),
  which is recorded in a flag. A repair that catches the exception and goes on
  is still rejected. Filesystem writes are only RECORDED (not blocked); a
  copy-on-write filesystem layer is the planned second step.
* Output (stdout/stderr) of the trial is held back and replayed only on commit.
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
import contextlib
import io
import os
import pickle
import select
import signal
import stat
import struct
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, List, Optional


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

    @property
    def committed(self) -> bool:
        return self.status is Status.COMMITTED


# ----------------------------------------------------------------------
# Effect detection (runs only inside the forked child)
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

_trial = {"active": False, "effects": [], "fs": []}
_hook_installed = False


def _audit_hook(event, args):
    if not _trial["active"]:
        return
    if event in _BLOCKED_EVENTS:
        _trial["effects"].append(event)          # flag: cannot be swallowed
        raise ExternalEffectBlocked(event)       # and stop the effect
    if event == "open":
        path, mode, flags = args
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                 (isinstance(flags, int) and flags & _WRITE_FLAGS)
        if writes and "__pycache__" not in str(path):
            _trial["fs"].append(f"open-write:{path}")
    elif event in _FS_EVENTS:
        _trial["fs"].append(f"{event}:{args[0] if args else ''}")


def _fd_offsets():
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


def _error_site(e):
    tb = e.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    return None if tb is None else (os.path.basename(tb.tb_frame.f_code.co_filename),
                                    tb.tb_lineno)


def _install_hook():
    global _hook_installed
    if not _hook_installed:
        sys.addaudithook(_audit_hook)            # cannot be removed; flag controls it
        _hook_installed = True


# ----------------------------------------------------------------------
# Child -> parent messages: length-prefixed pickles
# ----------------------------------------------------------------------
def _send(w, kind, payload):
    data = pickle.dumps((kind, payload))
    buf = struct.pack(">I", len(data)) + data
    while buf:
        n = os.write(w, buf)
        buf = buf[n:]


def _parse(buf):
    msgs = []
    while len(buf) >= 4:
        n = struct.unpack(">I", buf[:4])[0]
        if len(buf) < 4 + n:
            break
        msgs.append(pickle.loads(buf[4:4 + n]))
        buf = buf[4 + n:]
    return msgs, buf


def _child(w, state, repair, checks, resume, original_error, original_site):
    _install_hook()
    fd_before = _fd_offsets()
    _trial.update(active=True, effects=[], fs=[])
    out, err = io.StringIO(), io.StringIO()
    status = reason = None
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:                                              # Trial
                repair(state)
            except BaseException as e:
                if not _trial["effects"]:
                    status, reason = Status.REJECTED_REPAIR_ERROR, f"repair raised {e!r}"
            if status is None:                                # Validate
                for check in checks:
                    try:
                        ok, msg = check(state)
                    except BaseException as e:
                        ok, msg = False, f"check raised {e!r}"
                    if not ok:
                        status, reason = Status.REJECTED_CHECK, msg
                        break
    finally:
        _trial["active"] = False
    effects, fs = list(_trial["effects"]), list(_trial["fs"])
    fd_after = _fd_offsets()
    moved = []
    for fd, off in fd_before.items():
        if fd_after.get(fd, off) != off:
            moved.append(f"fd{fd}:{off}->{fd_after[fd]}")
            os.lseek(fd, off, os.SEEK_SET)        # undo the shared offset change
    if effects:                                               # even if swallowed
        status = Status.REJECTED_EXTERNAL_EFFECT
        reason = "external effect attempted: " + ", ".join(sorted(set(effects)))
    elif moved:
        status = Status.REJECTED_INHERITED_FD
        reason = "inherited file offset changed: " + ", ".join(moved)
    effects = effects + moved
    if status is not None:                                    # Restore side
        _send(w, "rejected", dict(status=status.name, reason=reason,
                                  effects=effects, fs=fs))
        os._exit(0)                                           # held-back output dropped

    # Commit: replay held-back output, tell the parent, then continue.
    sys.stdout.write(out.getvalue())
    sys.stderr.write(err.getvalue())
    sys.stdout.flush()
    sys.stderr.flush()
    _send(w, "validated", dict(effects=effects, fs=fs))
    try:
        result = resume(state)
        try:
            pickle.dumps(result)
            payload = dict(continuation="ok", result=result)
        except Exception as e:
            payload = dict(continuation="result_not_transferable", error=repr(e))
    except BaseException as e:
        again = original_error is not None and isinstance(e, original_error) and (
            original_site is None or _error_site(e) == original_site)
        payload = dict(continuation="original_error_again" if again else "new_error",
                       error=repr(e))
    _send(w, "continued", payload)
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
    original_site  -- optional (file basename, line) of the original crash; if given,
                      "again" requires the same type AND the same raise location
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
    buf, msgs = b"", []
    deadline = time.monotonic() + timeout
    timed_out = validated = finished = False
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
            if not validated and any(k == "validated" for k, _ in msgs):
                validated = True                   # verdict reached: new limit
                deadline = (None if continuation_timeout is None
                            else time.monotonic() + continuation_timeout)
        finished = True
    finally:
        if not finished or timed_out:              # timeout or Ctrl-C: no orphan
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        os.close(r)
        os.waitpid(pid, 0)

    got = {kind: payload for kind, payload in msgs}
    if "rejected" in got:
        p = got["rejected"]
        return Outcome(Status[p["status"]], p["reason"], effects=p["effects"],
                       fs_effects=p["fs"])
    if "validated" not in got:
        if timed_out:
            return Outcome(Status.REJECTED_TIMEOUT, f"no verdict within {timeout}s")
        return Outcome(Status.REJECTED_CHILD_DIED, "child ended without a verdict")
    v = got["validated"]
    out = Outcome(Status.COMMITTED, "all checks passed", effects=v["effects"],
                  fs_effects=v["fs"])
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