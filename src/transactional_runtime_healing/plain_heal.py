"""Plain healing: the HealBench way, with no fork and no undo.

The healing code runs in the live process on shallow copies of the crash frame's
namespaces (so rebinding a name does not touch the program), but objects are shared:
in-place changes take effect at once, and a failed or timed-out attempt does not revert
them. The returned bindings are written back by the instrumented handler and execution
continues after the failed statement.

This is the baseline for the evaluation (the "plain" scenario). It has no effect
detection and no validation. To compare it with the protocol, it still
  * runs the checks on the state after the code (`check_would_fail`: a repair the
    protocol would have rejected, plain healing applies anyway),
  * measures what the attempt changed in place (`inplace_changes`),
  * reports as `residue` what a FAILED attempt left behind (exception or timeout) and as
    `external_residue` the external effects it carried out (processes, network) and files it
    wrote. The protocol blocks the effects, so they cannot be residue there; plain healing has
    no protection and the audit hook only records them. This is the quantity of RQ1 and RQ2, and
  * reports separately, as `invalid_commit_changes`, the in-place changes of a repair that
    was applied although a check fails (the protocol would have rejected it).

With `guard=True` it is the "HealGuard-style" scenario: protected operations performed by
the healing code are stopped (taint.py, healing phase only; there is no validation point in
plain mode to follow the continuation). State the attempt had already changed is still not
undone.

Same interface as LiveHealer, same log format (`mode` = "plain").
"""
import os
import signal
import sys
import threading
import time
from typing import Callable, List, Optional

from transactional_runtime_healing import meter, taint
from transactional_runtime_healing import protocol as P
from transactional_runtime_healing.heal_point import checks_message, run_healing_code
from transactional_runtime_healing.instrument import HealContext, HealRecord, handled_by_caller
from transactional_runtime_healing.live_heal import LiveResult, LiveStatus, write_log


class _Timeout(BaseException):
    """Raised by the timer; not an Exception, so `except Exception` in the repair misses it."""


class PlainHealer:
    """propose(context) -> healing code or None; checks as in LiveHealer (evaluated only to
    report what the protocol would have done)."""

    def __init__(self, propose: Callable[[HealContext], Optional[str]],
                 checks: Optional[list] = None, timeout: float = 60.0, max_heals: int = 50,
                 log_path: Optional[str] = None, meter_state: bool = False,
                 guard=False, safe_paths=(), protected_events=None):
        self.propose = propose
        self.checks = checks or []
        self.timeout = timeout
        self.max_heals = max_heals
        self.log_path = log_path
        self.meter_state = meter_state
        # guard: False | "heal-phase" (True) | "taint". "heal-phase" stops only protected
        # operations of the healing code itself; "taint" also follows values the repair
        # introduced through the rest of the run (the fair HealGuard-style baseline).
        assert guard in (False, True, "heal-phase", "taint")
        self.guard = "heal-phase" if guard is True else guard
        self.protected_events = protected_events
        self._lineage: set = set()
        self._guard_obj = None
        self._trial_guard = None
        self.safe_paths = tuple(safe_paths)
        self.log: List[HealRecord] = []
        self._attempts = 0
        self._busy = False

    def __call__(self, exc, locals_, globals_, site):
        if self._busy or getattr(exc, "_heal_rejected", False):
            return None
        filename, lineno, func, statement = site
        clean = {k: v for k, v in locals_.items() if not k.startswith("__eh")}
        ctx = HealContext(exc, filename, lineno, func, statement, clean, globals_)
        target = sys._getframe(1)
        if handled_by_caller(exc, target, globals_, filename):
            self._record(ctx, None, LiveResult(LiveStatus.SKIPPED,
                                               "a caller handles this exception", mode="plain"))
            return None
        if self._attempts >= self.max_heals:
            return None
        self._busy = True
        try:
            code = self.propose(ctx)
            if code is None:
                self._record(ctx, None, None)
                return None
            self._attempts += 1
            return self._attempt(ctx, code, target)
        finally:
            self._busy = False

    def _attempt(self, ctx, code, target):
        before = meter.snapshot(target, ctx.globals_) if self.meter_state else None
        env = cl = cg = None
        failure = None
        old_handler = old_timer = None
        t0 = time.monotonic()
        use_alarm = (threading.current_thread() is threading.main_thread()
                     and hasattr(signal, "setitimer"))
        if use_alarm:
            def _alarm(*a):
                raise _Timeout()
            old_timer = signal.getitimer(signal.ITIMER_REAL)
            old_handler = signal.signal(signal.SIGALRM, _alarm)
            signal.setitimer(signal.ITIMER_REAL, self.timeout)
        guard = None
        if self.guard:                               # "HealGuard-style": protected operations of
            guard = taint.TaintGuard(safe_paths=self.safe_paths, events=self.protected_events,
                                     inherit=self._lineage)        # the healing code are stopped
            guard.start(taint.state_roots(target, ctx.globals_))
        P.begin_watch(record_only=True, guard=guard)   # record effects and file writes
        self._trial_guard = guard
        try:
            env, cl, cg, _ = run_healing_code(code, ctx.locals_, ctx.globals_)
        except _Timeout:
            failure = (LiveStatus.REJECTED_TIMEOUT, f"no result within {self.timeout}s")
        except Exception as e:
            failure = (LiveStatus.REJECTED_REPAIR_ERROR, repr(e))
        finally:
            effects, fs = P.end_watch()
            protected = P.protected_events()
            if use_alarm:                            # restore the program's own timer
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, old_handler)
                if old_timer[0] > 0:
                    left = max(old_timer[0] - (time.monotonic() - t0), 0.001)
                    signal.setitimer(signal.ITIMER_REAL, left, old_timer[1])

        if protected:                                # also when the repair swallowed the exception
            failure = (LiveStatus.REJECTED_PROTECTED_OPERATION, "; ".join(protected))
        changed = None
        if before is not None:
            changed = meter.diff(before, meter.snapshot(target, ctx.globals_))
        self._resume_continuation_watch()
        if failure is not None:                      # a failed attempt is NOT undone
            result = LiveResult(failure[0], failure[1], mode="plain",
                                residue=changed or [], inplace_changes=changed,
                                external_residue=effects + fs, effects=effects,
                                fs_effects=fs)
            self._record(ctx, code, result)
            try:
                ctx.exception._heal_rejected = True
            except Exception:
                pass
            return None
        msg = checks_message(self.checks, env)
        g = self._trial_guard
        if self.guard == "taint" and g is not None:  # follow the repair's values from now on
            g.finish(taint.state_roots(target, ctx.globals_), cl, cg)
            self._lineage = set(g.tainted)
            self._guard_obj = g
            self._resume_continuation_watch()
        result = LiveResult(LiveStatus.COMMITTED, "applied without validation", mode="plain",
                            bindings=cl, globals_updates=cg, changed=sorted({**cl, **cg}),
                            validated_at="none", inplace_changes=changed,
                            check_would_fail=msg or "", fs_effects=fs, effects=effects,
                            invalid_commit_changes=(changed or []) if msg else [],
                            external_residue=effects + fs if msg else [])
        self._record(ctx, code, result)
        return result

    def _resume_continuation_watch(self):
        """With guard="taint" the guard stays active for the rest of the run (blocking)."""
        if self.guard == "taint" and self._guard_obj is not None:
            P.begin_watch(record_only=True, guard=self._guard_obj, quiet=True)

    def _record(self, ctx, code, result):
        rec = HealRecord(ctx, code, result)
        self.log.append(rec)
        write_log(self.log_path, rec)