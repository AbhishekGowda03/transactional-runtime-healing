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
  * "The same error again" means: same exception type raised at the same place (file and
    line), as in protocol.py. By default this is just the next crash (a loop over bad
    inputs hits it once per item); the log's `repeat_count` says how often it happened
    before. `recurrence="reject"` gives the strict reading of proposal check 4, which
    rejects repairs that plain healing would get right inside loops.
  * After a rejection the same exception object is not offered to the enclosing statements
    again (they would otherwise get the original crash a second time and heal around it).
  * A crash whose exception a caller in the same file handles with its own `try` is not
    healed (logged as SKIPPED). Implicit handlers (for loops catching StopIteration,
    getattr defaults, contextlib.suppress, library code) are not seen.
  * A generator's validation point is its first yield after the repair.
  * Reading stdin in the trial is not undone: the child's buffer may take input from a
    pipe that the parent then never sees.
  * Each committed repair leaves one waiting process (the proxy) behind: n repairs in a
    run means n nested processes until the program ends.
  * Names that the healing code creates but the function does not use (helpers) are not
    added to the program's globals; they are listed in `leaked_globals`.
  * Log categories: "safety" rejections (error, check, effect, recurrence) versus
    "operational" ones (timeout, child died) - report them separately.
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
from transactional_runtime_healing.heal_point import checks_message, run_healing_code
from transactional_runtime_healing import meter
from transactional_runtime_healing.instrument import HealContext, HealRecord, handled_by_caller

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


# For the evaluation: a rejection by a safety rule is a different thing from one caused by
# the machinery (a slow repair killed by the timeout, a child that died).
_CATEGORY = {"COMMITTED": "committed", "TRIAL_ACCEPTED": "pending",
             "REJECTED_REPAIR_ERROR": "safety", "REJECTED_CHECK": "safety",
             "REJECTED_EXTERNAL_EFFECT": "safety", "REJECTED_RECURRENCE": "safety",
             "REJECTED_TIMEOUT": "operational", "REJECTED_CHILD_DIED": "operational",
             "SKIPPED": "skipped"}


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
    repeat_count: int = 0                        # earlier heals of the same error and place
    continuation_error: str = ""                 # the exception that ended the trial, if any
    leaked_globals: List[str] = field(default_factory=list)
    mode: str = "live"                           # "live" (protocol) or "plain" (HealBench-style)
    residue: List[str] = field(default_factory=list)   # state a failed attempt left behind
    inplace_changes: Optional[List[str]] = None  # plain mode: state changed in place; None = not measured
    external_residue: List[str] = field(default_factory=list)  # files written by a failed attempt
    check_would_fail: str = ""                   # plain mode: a check the repair would not pass
    invalid_commit_changes: List[str] = field(default_factory=list)  # plain mode: in-place changes
    #                                              of a repair that was applied although a check fails

    @property
    def category(self) -> str:
        return _CATEGORY[self.status.name]

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
    """What 'the same error' means (same as protocol.py): type and raise site."""
    return (type(exc).__name__, P.error_site(exc))


class LiveHealer:
    """Healer for `run_instrumented`: tries healing code live, commits at the validation point.

    propose(context) -> healing code (str) or None (no healing)
    checks           -- list of check(env) -> (ok, message), run on the state right after
                        the healing code
    timeout          -- seconds the parent waits for the validation point
    recurrence       -- what the same error (type and place) raised again before the
                        validation point means. "commit" (default): it is the next crash;
                        the pending repair is validated and the new crash is healed on its
                        own (a loop over bad inputs is normal). "reject": strict reading of
                        proposal check 4, the repair is rejected
    allow_events     -- audit events that are not blocked during the trial
    meter_state      -- measure residual state: fingerprint the program before each attempt
                        and again after a rejection (`residue` in the result and the log;
                        it must be empty - this checks the restore guarantee)
    log_path         -- JSON-lines file; one line per heal attempt, written by whichever
                        process learns the outcome (parent on reject, child on commit)
    """

    def __init__(self, propose: Callable[[HealContext], Optional[str]],
                 checks: Optional[list] = None, timeout: float = 60.0, max_heals: int = 50,
                 recurrence: str = "commit", allow_events=(), log_path: Optional[str] = None,
                 meter_state: bool = False):
        assert recurrence in ("commit", "reject")
        self.propose = propose
        self.checks = checks or []
        self.timeout = timeout
        self.max_heals = max_heals
        self.recurrence = recurrence
        self.allow_events = frozenset(allow_events)
        self.log_path = log_path
        self.meter_state = meter_state
        self.log: List[HealRecord] = []
        self._seen: Dict[tuple, int] = {}
        self._pending: Optional[_Pending] = None
        self._busy = False
        self._attempts = 0
        self._repeat = 0

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

        if getattr(exc, "_heal_rejected", False):    # a repair for this exception was rejected
            return None                              # already; enclosing statements do not retry
        if handled_by_caller(exc, target, globals_, filename):    # the program expects it
            self._record(ctx, None, LiveResult(LiveStatus.SKIPPED,
                                               "a caller handles this exception"))
            return None

        if self._pending is not None:                # a crash before the validation point
            if self.recurrence == "reject" and self._pending.signature == _signature(exc):
                self._reject_in_child("recurrence", f"{exc!r} happened again at line {lineno}")
            self._commit("next crash", repr(exc))

        if self._attempts >= self.max_heals:
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
        self._attempts += 1
        sig = _signature(ctx.exception)
        ctx_repeat = self._seen.get(sig, 0)
        self._seen[sig] = ctx_repeat + 1
        self._repeat = ctx_repeat
        sys.stdout.flush()
        sys.stderr.flush()
        offsets = P.fd_offsets()
        self._before = meter.snapshot(target, ctx.globals_) if self.meter_state else None
        self._target = target
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
        record = HealRecord(ctx, code, LiveResult(LiveStatus.TRIAL_ACCEPTED,
                                                  repeat_count=self._repeat))
        cap = P._FdCapture().start()
        self._pending = _Pending(target, ctx, record, w, cap, _signature(ctx.exception))
        P.begin_watch(allow=self.allow_events, on_effect=self._effect_in_child)
        try:
            env, cl, cg, _ = run_healing_code(code, ctx.locals_, ctx.globals_)
        except BaseException as e:
            self._reject_in_child("error", repr(e))
        msg = checks_message(self.checks, env)
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
        mon.register_callback(_TOOL_ID, ev.PY_YIELD, self._on_yield)
        mon.set_local_events(_TOOL_ID, frame.f_code, ev.PY_RETURN | ev.PY_YIELD)
        mon.set_events(_TOOL_ID, ev.PY_UNWIND)       # unwinding cannot be set per code object
        return True

    def _stop_monitoring(self, code):
        mon = sys.monitoring
        try:
            mon.set_local_events(_TOOL_ID, code, 0)
            mon.set_events(_TOOL_ID, 0)
            mon.register_callback(_TOOL_ID, mon.events.PY_RETURN, None)
            mon.register_callback(_TOOL_ID, mon.events.PY_UNWIND, None)
            mon.register_callback(_TOOL_ID, mon.events.PY_YIELD, None)
            mon.free_tool_id(_TOOL_ID)
        except ValueError:
            pass

    # -- validation point reached ----------------------------------------
    def _on_return(self, code, offset, retval):
        p = self._pending
        if p is not None and sys._getframe(1) is p.frame:
            self._commit("end of function")

    def _on_yield(self, code, offset, retval):
        """A generator hands control back: that is its validation point (waiting for the
        generator to finish would hold the program's output back indefinitely)."""
        p = self._pending
        if p is not None and sys._getframe(1) is p.frame:
            self._commit("generator yielded")

    def _on_unwind(self, code, offset, exc):
        p = self._pending
        if p is not None and sys._getframe(1) is p.frame:
            if self.recurrence == "reject" and p.signature == _signature(exc):
                self._reject_in_child("recurrence", f"{exc!r} left the function")
            self._commit("function left by an exception", repr(exc))

    def _at_exit(self):
        if self._pending is not None:
            self._commit("end of program")

    def _commit(self, where, error=""):
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
        res.continuation_error = error
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
            for s, h in saved.items():
                signal.signal(s, h)
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
                            fs_effects=info["fs"], repeat_count=self._repeat)
        result.external_residue = list(info["fs"])   # files written stay on disk
        if self._before is not None:                 # what did the rejected attempt leave?
            result.residue = meter.diff(self._before,
                                        meter.snapshot(self._target, ctx.globals_))
        try:
            ctx.exception._heal_rejected = True
        except Exception:
            pass
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
        write_log(self.log_path, rec)


def write_log(path, rec: HealRecord):
    """Append one JSON line describing a heal attempt (also used by the plain healer)."""
    if not path:
        return
    ctx, res = rec.context, rec.result
    line = dict(time=time.time(), pid=os.getpid(), file=ctx.filename, line=ctx.lineno,
                function=ctx.function, statement=ctx.statement,
                exception=repr(ctx.exception), code=rec.code,
                mode=res.mode if res else "none",
                status=res.status.name if res else "NO_PROPOSAL",
                reason=res.reason if res else "", effects=res.effects if res else [],
                fs_effects=res.fs_effects if res else [],
                validated_at=res.validated_at if res else "",
                repeat_count=res.repeat_count if res else 0,
                continuation_error=res.continuation_error if res else "",
                leaked_globals=res.leaked_globals if res else [],
                residue=res.residue if res else [],
                inplace_changes=res.inplace_changes if res else None,
                external_residue=res.external_residue if res else [],
                invalid_commit_changes=res.invalid_commit_changes if res else [],
                check_would_fail=res.check_would_fail if res else "",
                category=res.category if res else "none")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
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