"""Heal point: trial in a forked child, then replay in the live process.

This is the commit design intended for HealBench-style healing, where the
healing code runs on the crashed frame's namespaces and several crashes happen
in one run:

  1. TRIAL   - fork a child; run the healing code there on a copy of the crash
               frame's namespaces; run the checks. External effects are blocked
               (audit hook), output is held back, inherited file offsets are
               compared. Nothing the trial does can reach the live process,
               including in-place changes to shared objects (which HealBench
               leaves behind after a failed attempt).
  2. COMMIT  - if the trial is accepted, the live process runs the SAME healing
               code on its own namespaces (so in-place changes to shared objects
               take effect, as in HealBench), under the same effect detection,
               and the checks run again on the replayed result. The updated
               bindings are returned. Nothing is pickled.
  3. RESTORE - if the trial is rejected, nothing runs in the live process; the
               caller re-raises the original exception.

Replay risks (reported, not hidden):
  * divergence: the code may behave differently the second time (time, random
    numbers, process ids, files). Every name the code can reach (the frame's
    locals, the names the code mentions, and everything it changed) is
    fingerprinted in the trial and again after the replay; names whose
    fingerprints differ are listed in `diverged_names`. Objects that cannot be
    pickled are fingerprinted by type only. Set `fingerprint=False` to skip this
    for very large objects.
  * REPLAY_FAILED / REPLAY_BLOCKED / REPLAY_CHECK_FAILED: the code was accepted
    in the trial but raised, attempted an external effect, or failed a check in
    the live process. The live objects may then be partly changed (the same limit
    HealBench has today); the caller should re-raise the original exception.
  * the replay timeout uses SIGALRM, only in the main thread. The program's own
    SIGALRM handler and ITIMER_REAL timer are saved and restored (its timer is
    paused during the replay and re-armed with the remaining time). The timeout
    is a BaseException, so `except Exception` in the healing code cannot swallow
    it, but a bare `except:` still can.
  * the audit hook is process-wide: while a replay runs, subprocess and socket
    calls from every thread are blocked, not only from the healing code.
  * the limits of protocol.py (inherited sockets/pipes, native code, filesystem
    writes are only recorded).

The caller (the instrumented handler) applies `result.bindings` to the frame;
the paused function's locals cannot be assigned from here. Imported modules are
not returned as bindings.
"""
import hashlib
import os
import pickle
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from types import ModuleType
from typing import Any, Callable, Dict, List, Optional

from transactional_runtime_healing import protocol as P


class HealStatus(Enum):
    HEALED = "healed"
    REJECTED_HEAL_ERROR = "rejected: healing code raised an exception"
    REJECTED_CHECK = "rejected: a check failed"
    REJECTED_EXTERNAL_EFFECT = "rejected: external effect attempted"
    REJECTED_INHERITED_FD = "rejected: inherited file offset changed"
    REJECTED_TIMEOUT = "rejected: timeout during trial"
    REJECTED_CHILD_DIED = "rejected: child died during trial"
    REPLAY_FAILED = "accepted in trial, but raised in the live process"
    REPLAY_BLOCKED = "accepted in trial, but attempted an external effect in the live process"
    REPLAY_CHECK_FAILED = "accepted in trial, but a check failed on the replayed result"


_VERDICT_STATUS = {"error": HealStatus.REJECTED_HEAL_ERROR, "check": HealStatus.REJECTED_CHECK,
                   "effect": HealStatus.REJECTED_EXTERNAL_EFFECT,
                   "fd": HealStatus.REJECTED_INHERITED_FD}


@dataclass
class HealResult:
    status: HealStatus
    reason: str
    bindings: Dict[str, Any] = field(default_factory=dict)  # local bindings to write back
    globals_updates: Dict[str, Any] = field(default_factory=dict)
    changed: List[str] = field(default_factory=list)
    diverged: bool = False                    # trial and replay produced different values
    diverged_names: List[str] = field(default_factory=list)
    effects: List[str] = field(default_factory=list)
    fs_effects: List[str] = field(default_factory=list)
    fd_effects: List[str] = field(default_factory=list)

    @property
    def healed(self) -> bool:
        return self.status is HealStatus.HEALED


class _ReplayTimeout(BaseException):
    """Raised by the replay timer; not an Exception, so `except Exception` misses it."""


# ----------------------------------------------------------------------
def _run(code: str, locals_: dict, globals_: dict):
    """Run healing code on shallow copies of the namespaces.

    Returns (env, changed_local, changed_global, code_object). Changed = names
    whose binding was replaced (identity) or newly created; modules are skipped.
    In-place changes to shared mutable objects are not 'bindings'; they happen
    on the objects themselves.
    """
    env = dict(globals_)
    env.update(locals_)
    before = dict(env)
    code_obj = compile(code, "<healing code>", "exec")
    exec(code_obj, env)
    changed_local, changed_global = {}, {}
    for name, value in env.items():
        if name == "__builtins__" or isinstance(value, ModuleType):
            continue
        if name in before and before[name] is value:
            continue
        if name in locals_ or name not in globals_:
            changed_local[name] = value
        else:
            changed_global[name] = value
    return env, changed_local, changed_global, code_obj


def _fingerprint(value) -> str:
    try:
        return hashlib.sha1(pickle.dumps(value)).hexdigest()
    except Exception:
        return f"unpicklable:{type(value).__name__}"


def _fingerprints(env, locals_, changed, code_obj):
    names = set(locals_) | set(code_obj.co_names) | set(changed)
    return {n: _fingerprint(env[n]) for n in names
            if n in env and not isinstance(env[n], ModuleType)}


def _checks_message(checks, env):
    for check in checks:
        try:
            ok, msg = check(env)
        except BaseException as e:
            ok, msg = False, f"check raised {e!r}"
        if not ok:
            return msg
    return None


# ----------------------------------------------------------------------
def _child(w, code, locals_, globals_, checks, fingerprint):
    P.install_hook()
    rep = P.trial_in_child(lambda: _run(code, locals_, globals_),
                           lambda v: _checks_message(checks, v[0]))
    if rep.verdict is not None:
        P.send(w, "rejected", dict(verdict=rep.verdict, reason=rep.reason, effects=rep.effects,
                                   fs=rep.fs, fd=rep.fd))
        os._exit(0)
    env, cl, cg, code_obj = rep.value
    fp = _fingerprints(env, locals_, {**cl, **cg}, code_obj) if fingerprint else {}
    P.send(w, "accepted", dict(fp=fp, effects=rep.effects, fs=rep.fs, fd=rep.fd))
    os._exit(0)


def _trial(code, locals_, globals_, checks, timeout, fingerprint):
    sys.stdout.flush()
    sys.stderr.flush()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        _child(w, code, locals_, globals_, checks, fingerprint)
    os.close(w)
    msgs, timed_out = P.collect(r, pid, timeout)
    got = {k: v for k, v in msgs}
    for kind in ("rejected", "accepted"):
        if kind in got:
            return kind, got[kind]
    return ("timeout" if timed_out else "died"), None


def _replay(code, locals_, globals_, checks, replay_timeout, fingerprint):
    """Run the healing code in the live process, with effect detection and a timer."""
    main = threading.current_thread() is threading.main_thread()
    use_alarm = main and hasattr(signal, "setitimer")
    old_handler = old_timer = None
    t0 = time.monotonic()
    if use_alarm:
        def _alarm(*a):
            raise _ReplayTimeout()
        old_timer = signal.getitimer(signal.ITIMER_REAL)
        old_handler = signal.signal(signal.SIGALRM, _alarm)
        signal.setitimer(signal.ITIMER_REAL, replay_timeout)
    P.begin_watch()
    try:
        env, cl, cg, code_obj = _run(code, locals_, globals_)
        effects, fs = P.end_watch()
        if effects:                                  # swallowed by the healing code
            return ("blocked", effects, fs)
        msg = _checks_message(checks, env)
        fp = _fingerprints(env, locals_, {**cl, **cg}, code_obj) if fingerprint else {}
        return ("ok", cl, cg, fp, msg, fs)
    except _ReplayTimeout:
        P.end_watch()
        return ("failed", f"replay exceeded {replay_timeout}s")
    except BaseException as e:
        effects, fs = P.end_watch()
        if effects:
            return ("blocked", effects, fs)
        return ("failed", f"replay raised {e!r}")
    finally:
        P._trial["active"] = False
        if use_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)
            if old_timer[0] > 0:                     # re-arm the program's own timer
                left = max(old_timer[0] - (time.monotonic() - t0), 0.001)
                signal.setitimer(signal.ITIMER_REAL, left, old_timer[1])


def heal_point(locals_: dict, globals_: dict, healing_code: str,
               checks: Optional[List[Callable]] = None, timeout: float = 10.0,
               replay_timeout: float = 10.0, fingerprint: bool = True) -> HealResult:
    """Try `healing_code` on the crash frame's namespaces; commit only if accepted.

    locals_, globals_ -- namespaces of the crashed frame (not modified here)
    healing_code      -- source text (LLM-generated in the real system)
    checks            -- list of check(env) -> (ok, message); env = merged namespace
    """
    checks = checks or []
    kind, p = _trial(healing_code, locals_, globals_, checks, timeout, fingerprint)
    if kind == "timeout":
        return HealResult(HealStatus.REJECTED_TIMEOUT, f"no verdict within {timeout}s")
    if kind == "died":
        return HealResult(HealStatus.REJECTED_CHILD_DIED, "child ended without a verdict")
    if kind == "rejected":
        return HealResult(_VERDICT_STATUS[p["verdict"]], p["reason"], effects=p["effects"],
                          fs_effects=p["fs"], fd_effects=p["fd"])

    res = _replay(healing_code, locals_, globals_, checks, replay_timeout, fingerprint)
    if res[0] == "failed":
        return HealResult(HealStatus.REPLAY_FAILED, res[1])
    if res[0] == "blocked":
        return HealResult(HealStatus.REPLAY_BLOCKED,
                          "effect attempted only in the live process: " +
                          ", ".join(sorted(set(res[1]))), effects=res[1], fs_effects=res[2])
    _, cl, cg, fp, msg, fs = res
    diverged_names = sorted(n for n in set(fp) | set(p["fp"]) if fp.get(n) != p["fp"].get(n))
    common = dict(changed=sorted({**cl, **cg}), diverged=bool(diverged_names),
                  diverged_names=diverged_names, effects=p["effects"], fs_effects=fs,
                  fd_effects=p["fd"])
    if msg is not None:
        return HealResult(HealStatus.REPLAY_CHECK_FAILED, msg, **common)
    return HealResult(HealStatus.HEALED, "accepted in trial, committed by replay",
                      bindings=cl, globals_updates=cg, **common)