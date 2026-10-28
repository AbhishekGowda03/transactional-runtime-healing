"""Live healing: the trial child becomes the running program (design D).

This follows the proposal (section 7): the repair is tried in a forked child, and the
child *keeps running the program*. The original process waits as a proxy.

  Begin     at the crash, the process forks. The original process (parent) now only
            waits and forwards signals. The child runs the healing code and goes on
            executing the program from the crash point.
  Trial     everything the child does is held back or blocked: output goes to temp
            files, external effects (subprocess, sockets, ...) are rejected at once.
  Validate  the child reaches the VALIDATION POINT: the end of the function that
            contains the crash (return or exception leaving it), or the next crash
            elsewhere, or the end of the program. The repair is valid if
              1. the healing code raised no exception,
              2. the checks passed on the state after the healing code,
              3. no external effect was attempted (not even a swallowed one),
              4. the original error did not happen again before the validation point.
  Commit    the child writes the held-back output and tells the parent. The child is now
            the program; the parent waits for it to end and exits with its exit status.
  Restore   on any failure the parent kills the child, puts the inherited file offsets
            back and re-raises the original exception. The parent never ran any of the
            repair: its memory is exactly as it was at the crash.

Compared with `heal_point.py` (trial, then replay in the live process) nothing is run
twice, so there is no replay divergence, and the repair is judged together with the
code that runs after it.

Limits (stated, not hidden):
  * Linux, Python 3.12 (uses sys.monitoring for the end-of-function point).
  * The child has only the forking thread. If other threads exist, no healing is done.
  * Output is held back until the validation point, so it appears late.
  * Effects on inherited sockets/pipes, native code, and filesystem writes are not
    undone (writes are recorded in `fs_effects`; a copy-on-write layer is future work).
  * Every blocked event ends the trial, also if the program would have survived it.
    `allow_events` lets chosen events through (for example "ctypes.dlopen").
  * Ctrl-C reaches the child through the terminal; the proxy ignores SIGINT and
    forwards SIGTERM, SIGHUP, SIGQUIT, SIGUSR1, SIGUSR2.
  * If the child ends without a verdict (for example os._exit before the validation
    point) the repair is rejected.
"""
import atexit
import json
import os
import select
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from transactional_runtime_healing import protocol as P
from transactional_runtime_healing.heal_point import _checks_message, _run
from transactional_runtime_healing.instrument import HealContext, HealRecord

_TOOL_ID = 3                                      # sys.monitoring tool id
_FORWARD = [signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGUSR1, signal.SIGUSR2]


class LiveStatus(Enum):
    TRIAL_ACCEPTED = "trial running (in the child)"
    COMMITTED = "committed: validation point reached"
    REJECTED_REPAIR_ERROR = "rejected: healing code raised an exception"
    REJECTED_CHECK = "rejected: a check failed"
    REJECTED_EXTERNAL_EFFECT = "rejected: external effect attempted"
    REJECTED_RECURRENCE = "rejected: the original error happened again"
    REJECTED_TIMEOUT = "rejected: no verdict in time"
    REJECTED_CHILD_DIED = "rejected: child ended without a verdict"
    SKIPPED = "not tried"


@dataclass
class LiveResult:
    status: LiveStatus
    reason: str = ""
    bindings: Dict[str, Any] = field(default_factory=dict)
    globals_updates: Dict[str, Any] = field(default_factory=dict)
    changed: List[str] = field(default_factory=list)
    effects: List[str] = field(default_factory=list)
    fs_effects: List[str] = field(default_factory=list)
    validated_at: str = ""

    @property
    def healed(self) -> bool:
        return self.status in (LiveStatus.TRIAL_ACCEPTED, LiveStatus.COMMITTED)


class _Pending:
    """State of a trial that is running in this (child) process."""

    def __init__(self, frame, ctx, record, w, cap, signature):
        self.frame, self.ctx, self.record, self.w = frame, ctx, record, w
        self.cap, self.signature = cap, signature
        self.t0 = time.monotonic()


def _signature(exc):
    return (type(exc).__name__, str(exc))


class LiveHealer:
    """Healer for `run_instrumented`: tries healing code live, commits at the validation point.

    propose(context) -> healing code (str) or None (no healing)
    checks           -- list of check(env) -> (ok, message), run on the state right after
                        the healing code
    timeout          -- seconds the parent waits for the validation point
    recurrence       -- "reject": the original error again before the validation point
                        rejects the repair (proposal check 4); "allow": it does not
    allow_events     -- audit events that are not blocked during the trial
    log_path         -- JSON-lines file; one line per heal attempt, written by whichever
                        process learns the outcome (parent on reject, child on commit)
    """

    def __init__(self, propose: Callable[[HealContext], Optional[str]],
                 checks: Optional[list] = None, timeout: float = 60.0, max_heals: int = 50,
                 recurrence: str = "reject", allow_events=(), log_path: Optional[str] = None):
        assert recurrence in ("reject", "allow")
        self.propose = propose
        self.checks = checks or []
        self.timeout = timeout
        self.max_heals = max_heals
        self.recurrence = recurrence
        self.allow_events = frozenset(allow_events)
        self.log_path = log_path
        self.log: List[HealRecord] = []
        self._pending: Optional[_Pending] = None
        self._busy = False

    # ------------------------------------------------------------------
    # called by the instrumented code at a crash
    # ------------------------------------------------------------------
    def __call__(self, exc, locals_, globals_, site):
        if self._busy:
            return None
        filename, lineno, func, statement = site
        clean = {k: v for k, v in locals_.items() if not k.startswith("__eh")}
        ctx = HealContext(exc, filename, lineno, func, statement, clean, globals_)
        target = sys._getframe(1)                    # the function that contains the crash

        if self._pending is not None:                # a crash before the validation point
            if self.recurrence == "reject" and self._pending.signature == _signature(exc):
                self._reject_in_child("recurrence", f"{exc!r} happened again at line {lineno}")
            self._commit("next crash")

        if len(self.log) >= self.max_heals:
            return None
        skip = self._unsupported()
        if skip:
            self._record(ctx, None, LiveResult(LiveStatus.SKIPPED, skip))
            return None

        self._busy = True
        try:
            code = self.propose(ctx)
            if code is None:
                self._record(ctx, None, None)
                return None
            return self._attempt(ctx, code, target)
        finally:
            self._busy = False

    @staticmethod
    def _unsupported():
        if threading.active_count() > 1:
            return "other threads exist; the forked child would not have them"
        if threading.current_thread() is not threading.main_thread():
            return "not in the main thread"
        if not hasattr(sys, "monitoring"):
            return "Python 3.12 or newer is required"
        return ""

    # ------------------------------------------------------------------
    def _attempt(self, ctx, code, target):
        sys.stdout.flush()
        sys.stderr.flush()
        offsets = P.fd_offsets()
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            return self._child_begin(ctx, code, target, w)
        os.close(w)
        return self._parent_wait(ctx, code, pid, r, offsets)

    # ------------------------------------------------------------------
    # child: runs the repair, then keeps running the program
    # ------------------------------------------------------------------
    def _child_begin(self, ctx, code, target, w):
        record = HealRecord(ctx, code, LiveResult(LiveStatus.TRIAL_ACCEPTED))
        cap = P._FdCapture().start()
        self._pending = _Pending(target, ctx, record, w, cap, _signature(ctx.exception))
        P.begin_watch(allow=self.allow_events, on_effect=self._effect_in_child)
        try:
            env, cl, cg, _ = _run(code, ctx.locals_, ctx.globals_)
        except BaseException as e:
            self._reject_in_child("error", repr(e))
        msg = _checks_message(self.checks, env)
        if msg is not None:
            self._reject_in_child("check", msg)
        if not self._start_monitoring(target):
            self._reject_in_child("error", "sys.monitoring tool id is not available")
        atexit.register(self._at_exit)
        res = record.result
        res.bindings, res.globals_updates, res.changed = cl, cg, sorted({**cl, **cg})
        self.log.append(record)
        return res                                   # the program goes on from the crash point

    def _effect_in_child(self, event):
        self._reject_in_child("effect", f"attempted {event}", effects=[event])

    def _reject_in_child(self, verdict, reason, effects=()):
        p = self._pending
        P.send(p.w, "reject", dict(verdict=verdict, reason=reason, effects=list(effects),
                                   fs=list(P._trial["fs"])))
        os._exit(0)                                  # held-back output is dropped with it

    def _start_monitoring(self, frame) -> bool:
        mon = sys.monitoring
        try:
            mon.use_tool_id(_TOOL_ID, "transactional-healing")
        except ValueError:
            return False
        ev = mon.events
        mon.register_callback(_TOOL_ID, ev.PY_RETURN, self._on_return)
        mon.register_callback(_TOOL_ID, ev.PY_UNWIND, self._on_unwind)
        mon.set_local_events(_TOOL_ID, frame.f_code, ev.PY_RETURN)
        mon.set_events(_TOOL_ID, ev.PY_UNWIND)       # unwinding cannot be set per code object
        return True

    def _stop_monitoring(self, code):
        mon = sys.monitoring
        try:
            mon.set_local_events(_TOOL_ID, code, 0)
            mon.set_events(_TOOL_ID, 0)
            mon.register_callback(_TOOL_ID, mon.events.PY_RETURN, None)
            mon.register_callback(_TOOL_ID, mon.events.PY_UNWIND, None)
            mon.free_tool_id(_TOOL_ID)
        except ValueError:
            pass

    # -- validation point reached ----------------------------------------
    def _on_return(self, code, offset, retval):
        p = self._pending
        if p is not None and sys._getframe(1) is p.frame:
            self._commit("end of function")

    def _on_unwind(self, code, offset, exc):
        p = self._pending
        if p is not None and sys._getframe(1) is p.frame:
            if self.recurrence == "reject" and p.signature == _signature(exc):
                self._reject_in_child("recurrence", f"{exc!r} left the function")
            self._commit("function left by an exception")

    def _at_exit(self):
        if self._pending is not None:
            self._commit("end of program")

    def _commit(self, where):
        p = self._pending
        self._pending = None
        self._stop_monitoring(p.frame.f_code)
        atexit.unregister(self._at_exit)
        p.cap.stop()
        P.write_all(1, p.cap.out)                    # held-back output, now released
        P.write_all(2, p.cap.err)
        effects, fs = P.end_watch()
        res = p.record.result
        res.status, res.validated_at, res.fs_effects = LiveStatus.COMMITTED, where, fs
        res.reason = f"validation point: {where}"
        self._write_log(p.record)
        P.send(p.w, "commit", dict(where=where))
        os.close(p.w)

    # ------------------------------------------------------------------
    # parent: proxy while the child is on trial; proxy for the program after commit
    # ------------------------------------------------------------------
    def _parent_wait(self, ctx, code, pid, r, offsets):
        saved = {}

        def forward(signum, frame):
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                pass
        for s in _FORWARD:
            saved[s] = signal.signal(s, forward)
        saved[signal.SIGINT] = signal.signal(signal.SIGINT, signal.SIG_IGN)

        verdict, buf = None, b""
        deadline = time.monotonic() + self.timeout
        try:
            while verdict is None:
                left = deadline - time.monotonic()
                if left <= 0:
                    verdict = ("reject", dict(verdict="timeout", reason=f"no verdict within "
                                              f"{self.timeout}s", effects=[], fs=[]))
                    break
                ready, _, _ = select.select([r], [], [], left)
                if not ready:
                    continue
                chunk = os.read(r, 65536)
                if not chunk:
                    verdict = ("reject", dict(verdict="died", reason="child ended without a "
                                              "verdict", effects=[], fs=[]))
                    break
                msgs, buf = P._parse(buf + chunk)
                for kind, payload in msgs:
                    if kind in ("commit", "reject"):
                        verdict = (kind, payload)
                        break
        except BaseException:
            _kill(pid)
            raise
        os.close(r)

        if verdict[0] == "commit":
            self._proxy_until_exit(pid)              # does not return
        # Restore: the child is discarded, the parent is exactly as before the crash.
        _kill(pid)
        for fd, off in offsets.items():
            try:
                os.lseek(fd, off, os.SEEK_SET)
            except OSError:
                pass
        for s, h in saved.items():
            signal.signal(s, h)
        info = verdict[1]
        status = {"error": LiveStatus.REJECTED_REPAIR_ERROR, "check": LiveStatus.REJECTED_CHECK,
                  "effect": LiveStatus.REJECTED_EXTERNAL_EFFECT,
                  "recurrence": LiveStatus.REJECTED_RECURRENCE,
                  "timeout": LiveStatus.REJECTED_TIMEOUT,
                  "died": LiveStatus.REJECTED_CHILD_DIED}[info["verdict"]]
        result = LiveResult(status, info["reason"], effects=info["effects"],
                            fs_effects=info["fs"])
        self._record(ctx, code, result)
        return None                                  # caller re-raises the original exception

    def _proxy_until_exit(self, pid):
        while True:
            try:
                _, status = os.waitpid(pid, 0)
                break
            except InterruptedError:
                continue
        if os.WIFEXITED(status):
            os._exit(os.WEXITSTATUS(status))
        sig = os.WTERMSIG(status)                    # the program died from a signal: do too
        signal.signal(sig, signal.SIG_DFL)
        os.kill(os.getpid(), sig)
        os._exit(128 + sig)

    # ------------------------------------------------------------------
    def _record(self, ctx, code, result):
        rec = HealRecord(ctx, code, result)
        self.log.append(rec)
        self._write_log(rec)

    def _write_log(self, rec: HealRecord):
        if not self.log_path:
            return
        ctx, res = rec.context, rec.result
        line = dict(time=time.time(), pid=os.getpid(), file=ctx.filename, line=ctx.lineno,
                    function=ctx.function, statement=ctx.statement,
                    exception=repr(ctx.exception), code=rec.code,
                    status=res.status.name if res else "NO_PROPOSAL",
                    reason=res.reason if res else "", effects=res.effects if res else [],
                    fs_effects=res.fs_effects if res else [],
                    validated_at=res.validated_at if res else "")
        fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (json.dumps(line) + "\n").encode())
        finally:
            os.close(fd)


def _kill(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass