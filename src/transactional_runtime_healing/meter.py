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


def _stdlib_names():
    """Names of the standard library's top-level modules (sys.stdlib_module_names exists from
    Python 3.10; for 3.9 the names are read from the standard library folder)."""
    names = getattr(sys, "stdlib_module_names", None)
    if names is not None:
        return names
    import sysconfig
    found = set(sys.builtin_module_names)
    root = sysconfig.get_paths()["stdlib"]
    try:
        for entry in os.listdir(root):
            if entry == "site-packages":
                continue
            found.add(entry[:-3] if entry.endswith(".py") else entry.split(".")[0])
    except OSError:
        pass
    return frozenset(found)


_STDLIB = _stdlib_names()

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


def _structural(v) -> str:
    """Fingerprint that does not depend on object identity (so the same module imported in
    two interpreters gives the same value): functions by name and bytecode, classes by name."""
    if isinstance(v, _CALLABLE_TYPES):
        code = getattr(v, "__code__", None)
        body = hashlib.sha1(code.co_code).hexdigest() if code is not None else ""
        return f"{type(v).__name__}:{getattr(v, '__qualname__', '')}:{body}"
    if isinstance(v, type):
        return f"class:{v.__qualname__}"
    return _fingerprint(v)


def module_structure(modname: str) -> Dict[str, str]:
    """Identity-free fingerprint of the attributes of a loaded module (same attribute
    filter as the snapshot)."""
    mod = sys.modules.get(modname)
    out: Dict[str, str] = {}
    if mod is None:
        return out
    is_std = modname.split(".")[0] in _STDLIB
    try:
        items = list(vars(mod).items())
    except TypeError:
        return out
    for attr, value in items:
        if attr in _SKIP_MODULE_ATTRS or isinstance(value, types.ModuleType):
            continue
        if is_std and not (isinstance(value, _CALLABLE_TYPES + (type,)) or
                           isinstance(value, _PLAIN)):
            continue
        try:
            out[attr] = _structural(value)
        except Exception:
            out[attr] = "unfingerprintable"
    return out


def _fresh_structures(names: List[str]) -> Dict[str, Dict[str, str]]:
    """What a pristine interpreter has in these modules (one subprocess for all of them)."""
    import json
    import os
    import subprocess
    src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = ("import sys, json\nsys.path.insert(0, %r)\n"
            "from transactional_runtime_healing import meter\nout = {}\n"
            "for n in %r:\n    try:\n        __import__(n)\n"
            "        out[n] = meter.module_structure(n)\n    except BaseException:\n        pass\n"
            "print(json.dumps(out))\n") % (src, list(names))
    try:
        p = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                           timeout=30)
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        return {}


def _new_module_changes(new_modules: List[str]) -> List[str]:
    """Attributes of modules imported during the attempt that differ from a fresh import.
    Importing a library is not a change, but patching one right after importing it is."""
    fresh = _fresh_structures(new_modules)
    changed: List[str] = []
    for name in new_modules:
        if name not in fresh:                    # could not be imported afresh: not judged
            continue
        now = module_structure(name)
        for attr in set(now) | set(fresh[name]):
            if now.get(attr) != fresh[name].get(attr):
                changed.append(f"module:{name}.{attr}")
    return changed


def _module_snapshot(snap: Dict[str, str]):
    stdlib = _STDLIB
    for modname, mod in list(sys.modules.items()):
        if mod is None or modname.split(".")[0] == _OWN or modname == "__main__":
            continue
        try:
            items = list(vars(mod).items())
        except TypeError:
            continue
        is_std = modname.split(".")[0] in stdlib
        snap[f"@loaded:{modname}"] = "1"
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
    """Names whose fingerprint differs, or that appeared or disappeared.

    A module that was imported between the two snapshots is compared with a fresh import
    in a clean interpreter: importing a library is not a change, patching it is."""
    loaded_before = {k for k in before if k.startswith("@loaded:")}
    new_modules = [k[8:] for k in after if k.startswith("@loaded:") and k not in loaded_before] \
        if loaded_before else []
    prefixes = tuple(f"module:{m}." for m in new_modules)
    out = [k for k in set(before) | set(after)
           if not k.startswith("@loaded:") and before.get(k) != after.get(k)
           and not (prefixes and k.startswith(prefixes))]
    if new_modules:
        out += _new_module_changes(new_modules)
    return sorted(out)