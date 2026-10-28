"""Residual-state meter (RQ1, RQ2, H1).

`snapshot()` takes a fingerprint of everything a repair could have changed in the
running program; `diff()` lists what differs between two snapshots. Taken before a heal
attempt and again after a failed one, the difference is the RESIDUAL STATE the attempt left
behind.

What is covered
  * module globals of the program, and the locals of the crashed frame and of every caller
    in an instrumented file (this is where objects passed as arguments are reachable from),
  * module attributes of imported modules. Modules outside the standard library are
    fingerprinted completely (this covers library settings such as a flag or a patched
    function). For standard-library modules only callables (by identity) and plain numbers,
    strings, booleans and None are fingerprinted, which is cheap and catches monkeypatches,
  * functions, classes and builtins by identity (and, for classes, their own attributes),
    so rebinding a name to another callable is seen,
  * the process environment, the working directory, sys.path and the offsets of open
    regular files,
  * an explicit list of process-wide settings that repairs use to silence errors and that
    live in objects or C state: warnings.filters, logging levels (root, every named logger,
    logging.disable), random's state, the recursion limit, the switch interval, the default
    socket timeout, every signal handler, the identity of sys.stdout/stderr, the decimal
    context, and numpy's error settings when numpy is loaded.
Values are fingerprinted with a hash of their pickle. Objects that cannot be pickled
(locks, sessions, loggers, files) are fingerprinted structurally: their attributes are
walked, with a depth limit and a guard against cycles, and a leaf that cannot be walked
counts by type only.

What is not covered (stated limits)
  * state outside the Python process: files written, network, other processes (files
    written are reported separately as `external_residue`, from the audit hook),
  * state held only by C extensions, and objects reachable only from frames that are not in
    an instrumented file,
  * false positives are possible: values that are equal but pickle differently (a set with
    another insertion history, different aliasing) count as changed.
The meter compares states of the SAME process, so it has to run in the process that was
paused at the crash (the parent in the live protocol). There, an empty result after a
rejection is a design property (the parent never ran the repair); the meter verifies it, and
the real exposure of the protocol is `external_residue`.
Cost: a snapshot pickles every global and local, which is slow for large objects; leave the
meter off in runs that measure overhead.
"""
import hashlib
import os
import pickle
import sys
import types
import warnings
from typing import Dict, List, Optional

from transactional_runtime_healing import protocol as P
from transactional_runtime_healing.instrument import HANDLER_TABLES

_OWN = "transactional_runtime_healing"
_CALLABLE_TYPES = (types.FunctionType, types.BuiltinFunctionType, types.MethodType,
                   types.MethodDescriptorType, types.WrapperDescriptorType)
_PLAIN = (int, float, complex, str, bytes, bool, type(None))
_SKIP_MODULE_ATTRS = {"__builtins__", "__spec__", "__loader__", "__cached__", "__file__",
                      "__path__", "__doc__", "__package__", "__name__"}
_MAX_DEPTH = 6


def _ident(v) -> str:
    return f"{type(v).__name__}:{getattr(v, '__qualname__', '')}:{id(v)}"


def _deep(v, depth=0, seen=None) -> str:
    """Structural fingerprint for objects that cannot be pickled."""
    if seen is None:
        seen = set()
    if isinstance(v, _PLAIN):
        return repr(v)
    if isinstance(v, types.ModuleType):
        return f"module:{v.__name__}"
    if isinstance(v, _CALLABLE_TYPES):
        return _ident(v)
    if id(v) in seen:
        return f"cycle:{type(v).__name__}"
    if depth >= _MAX_DEPTH:
        return f"deep:{type(v).__name__}"
    seen = seen | {id(v)}
    if isinstance(v, type):
        parts = [f"{k}={_ident(a) if callable(a) or isinstance(a, (staticmethod, classmethod, property)) else _deep(a, depth + 1, seen)}"
                 for k, a in sorted(vars(v).items()) if not k.startswith("__")]
        return f"class:{_ident(v)}[{';'.join(parts)}]"
    if isinstance(v, (list, tuple)):
        return f"{type(v).__name__}[{','.join(_deep(i, depth + 1, seen) for i in v)}]"
    if isinstance(v, (set, frozenset)):
        return f"{type(v).__name__}[{','.join(sorted(_deep(i, depth + 1, seen) for i in v))}]"
    if isinstance(v, dict):
        return "dict[" + ",".join(sorted(f"{_deep(k, depth + 1, seen)}:{_deep(x, depth + 1, seen)}"
                                         for k, x in v.items())) + "]"
    attrs = {}
    try:
        attrs.update(vars(v))
    except TypeError:
        pass
    for cls in type(v).__mro__:
        for slot in getattr(cls, "__slots__", ()) if not isinstance(
                getattr(cls, "__slots__", ()), str) else (cls.__slots__,):
            if hasattr(v, slot):
                attrs[slot] = getattr(v, slot)
    if not attrs:
        return f"leaf:{type(v).__name__}"
    return f"obj:{type(v).__name__}[" + ",".join(
        f"{k}={_deep(a, depth + 1, seen)}" for k, a in sorted(attrs.items())) + "]"


def _fingerprint(v) -> str:
    if isinstance(v, (types.ModuleType,)) or isinstance(v, _CALLABLE_TYPES):
        return _ident(v)
    if isinstance(v, type):
        return _deep(v)
    try:
        return hashlib.sha1(pickle.dumps(v, protocol=4)).hexdigest()
    except Exception:
        try:
            return "S:" + hashlib.sha1(_deep(v).encode()).hexdigest()
        except Exception:
            return f"unwalkable:{type(v).__name__}"


def _process_settings(snap: Dict[str, str], filters):
    """Process-wide settings (explicit list, see module docstring)."""
    import logging
    import random
    import signal
    import socket

    def put(key, fn):
        try:
            snap[f"@setting:{key}"] = _fingerprint(fn())
        except Exception as e:
            snap[f"@setting:{key}"] = f"error:{type(e).__name__}"

    put("warnings.filters", lambda: filters)
    put("logging.root.level", lambda: logging.getLogger().level)
    put("logging.root.handlers", lambda: [(type(h).__name__, h.level) for h in logging.getLogger().handlers])
    put("logging.disable", lambda: logging.root.manager.disable)
    put("logging.loggers", lambda: {n: (getattr(l, "level", None), getattr(l, "disabled", None),
                                        getattr(l, "propagate", None))
                                    for n, l in logging.root.manager.loggerDict.items()})
    put("random.state", random.getstate)
    put("sys.recursionlimit", sys.getrecursionlimit)
    put("sys.switchinterval", sys.getswitchinterval)
    put("socket.timeout", socket.getdefaulttimeout)
    put("signal.handlers", lambda: {int(sig): _ident(signal.getsignal(sig))
                                    if callable(signal.getsignal(sig)) else repr(signal.getsignal(sig))
                                    for sig in signal.valid_signals()
                                    if sig not in (signal.SIGKILL, signal.SIGSTOP)})
    put("sys.stdout", lambda: id(sys.stdout))
    put("sys.stderr", lambda: id(sys.stderr))
    if "decimal" in sys.modules:
        put("decimal.context", lambda: repr(sys.modules["decimal"].getcontext()))
    if "numpy" in sys.modules:
        put("numpy.errstate", lambda: sys.modules["numpy"].geterr())


def _own_name(name: str) -> bool:
    return name.startswith("__heal") or name.startswith("__eh") or name == "__builtins__"


def _module_snapshot(snap: Dict[str, str]):
    stdlib = sys.stdlib_module_names
    for modname, mod in list(sys.modules.items()):
        if mod is None or modname.split(".")[0] == _OWN or modname == "__main__":
            continue
        try:
            items = list(vars(mod).items())
        except TypeError:
            continue
        is_std = modname.split(".")[0] in stdlib
        for attr, value in items:
            if attr in _SKIP_MODULE_ATTRS or isinstance(value, types.ModuleType):
                continue
            if is_std and not (isinstance(value, _CALLABLE_TYPES + (type,)) or
                               isinstance(value, _PLAIN)):
                continue
            snap[f"module:{modname}.{attr}"] = (_ident(value) if is_std and not isinstance(
                value, _PLAIN) else _fingerprint(value))


_warm = {"modules": -1}


def snapshot(frame=None, globals_: Optional[dict] = None, environment: bool = True,
             modules: bool = True) -> Dict[str, str]:
    """Fingerprints of the state a repair could have changed (see module docstring).

    Fingerprinting has side effects in some libraries: pickling a class looks it up by
    name, which makes `ast` create its deprecated aliases (Ellipsis, Num, ...) and fills
    caches of typing aliases in pandas. The first snapshot in a process (and the first after
    new modules were imported) would then differ from the next one. So a throw-away pass
    runs first whenever the set of loaded modules has changed since the last snapshot."""
    # Read the program's warning filters BEFORE silencing warnings for the pickling below
    # (inside catch_warnings the list is a modified copy).
    filters = list(warnings.filters)
    with warnings.catch_warnings():          # pickling touches deprecated names (ast.Num ...)
        warnings.simplefilter("ignore")
        if _warm["modules"] != len(sys.modules):
            _take(frame, globals_, environment, modules, filters)
        snap = _take(frame, globals_, environment, modules, filters)
    _warm["modules"] = len(sys.modules)
    return snap


def _take(frame, globals_, environment, modules, filters) -> Dict[str, str]:
    snap: Dict[str, str] = {}
    seen_globals = set()

    def add_globals(g):
        if id(g) in seen_globals:
            return
        seen_globals.add(id(g))
        for name, value in list(g.items()):
            if not _own_name(name):
                snap[f"global:{name}"] = _fingerprint(value)

    if globals_ is not None:
        add_globals(globals_)
    depth, f = 0, frame
    while f is not None:
        if f.f_code.co_filename in HANDLER_TABLES:
            add_globals(f.f_globals)
            if f.f_code.co_name != "<module>":           # module locals are the globals
                for name, value in list(f.f_locals.items()):
                    if not _own_name(name):
                        snap[f"local:{depth}:{f.f_code.co_name}:{name}"] = _fingerprint(value)
        depth += 1
        f = f.f_back
    if modules:
        _module_snapshot(snap)
    if environment:
        _process_settings(snap, filters)
        snap["@environ"] = _fingerprint(dict(os.environ))
        snap["@cwd"] = os.getcwd()
        snap["@sys.path"] = _fingerprint(list(sys.path))
        for fd, off in P.fd_offsets().items():
            snap[f"@fd:{fd}"] = str(off)
    return snap


def diff(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    """Names whose fingerprint differs, or that appeared or disappeared."""
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))