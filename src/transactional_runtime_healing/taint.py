"""Protected-operations check (proposal check 2): an observation-based approximation.

HealGuard tracks taint through the healing code and flags healing-influenced values that
reach protected operations. This module approximates that without data-flow tracking, using
the audit hook and VALUE MATCHING:

  Phase "healing"      any protected operation attempted while the healing code runs is
                       healing-influenced by definition: the repair itself deletes, writes,
                       connects or starts a command. It is stopped and reported.
  Phase "continuation" after the healing code, the values it introduced are collected:
                       every string that is in the program state afterwards (new bindings,
                       and anything it changed in place) but was not there before. A
                       protected operation whose arguments equal or contain such a value
                       (or one of its path components) is healing-influenced.

Protected operations (audit events): file writes (open for writing), remove, rename,
rmdir, rmtree, move, truncate, chmod, chown, link, symlink, mkdir, sqlite3.connect,
socket.connect/sendto, subprocess.Popen, os.system, os.posix_spawn.

Where it differs from HealGuard:
  * False negatives: a value that is transformed (hashed, encoded, split into pieces, built
    from digits) before it reaches the operation no longer matches. Only strings of at least
    `min_len` characters are tracked; numbers are not.
  * False positives: a program that coincidentally uses a string equal to a tainted one
    (a default value the repair introduced and the program also uses elsewhere).
  * Only operations seen by the audit hook; native code is not seen.
  * The state walk is bounded (depth, node count; breadth first), so very large states are
    covered partly; `TaintGuard.truncated` says when that happened.
  * Matching is deliberately conservative: a value must equal an argument, be a whole path
    component of at least 6 characters, or occur as a whole token (at least 8 characters)
    inside an argument. Short common words therefore do not reject safe repairs, and a
    tainted value shorter than that is not followed (false negative).
The false-negative and false-positive rates against HealGuard are measured in the
evaluation, not assumed here.
"""
import os
import types
import re
from collections import deque
from typing import Dict, Iterable, List, Optional, Set

from transactional_runtime_healing.instrument import HANDLER_TABLES
from transactional_runtime_healing.protocol import PROTECTED_EVENTS   # one definition

_MAX_DEPTH = 8
_MAX_NODES = 200_000
_SUBSTRING_MIN = 8             # a value must be this long to be searched for INSIDE an argument
_COMPONENT_MIN = 6             # ... or to be matched against one path component
_SUBSTRING_LIMIT = 5000        # substring matching only for small sets


def state_roots(frame, globals_: dict) -> List[dict]:
    """Namespaces reachable from the crash: globals and the locals of every instrumented
    caller frame (this is where objects passed as arguments are reachable from)."""
    roots = [globals_]
    f = frame
    while f is not None:
        if f.f_code.co_filename in HANDLER_TABLES and f.f_code.co_name != "<module>":
            roots.append(dict(f.f_locals))
        f = f.f_back
    return roots


def leaves(roots: Iterable, min_len: int = 4, stats: Optional[dict] = None) -> Set[str]:
    """All strings (and paths, and decodable bytes) of at least `min_len` characters that are
    reachable from the roots, within the depth and node limits. The walk is breadth first, so
    a node limit cuts off the deepest part of every root and not whole roots at the end of the
    list. `stats["truncated"]` is set when a limit was hit."""
    out: Set[str] = set()
    seen: Set[int] = set()
    stack = deque((r, 0) for r in roots)
    nodes = 0
    while stack:
        if nodes >= _MAX_NODES:
            if stats is not None:
                stats["truncated"] = True
            break
        v, depth = stack.popleft()
        nodes += 1
        if isinstance(v, str):
            if len(v) >= min_len:
                out.add(v)
            continue
        if isinstance(v, bytes):
            try:
                s = v.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if len(s) >= min_len:
                out.add(s)
            continue
        if isinstance(v, os.PathLike):
            try:
                s = os.fspath(v)
                s = s if isinstance(s, str) else s.decode()
                if len(s) >= min_len:
                    out.add(s)
            except Exception:
                pass
            continue
        if v is None or isinstance(v, (int, float, bool, complex, types.ModuleType, type,
                                       types.FunctionType, types.BuiltinFunctionType)):
            continue
        if id(v) in seen:
            continue
        if depth >= _MAX_DEPTH:
            if stats is not None:
                stats["truncated"] = True
            continue
        seen.add(id(v))
        if isinstance(v, dict):
            for k, x in list(v.items()):
                stack.append((k, depth + 1))
                stack.append((x, depth + 1))
        elif isinstance(v, (list, tuple, set, frozenset)):
            for x in list(v):
                stack.append((x, depth + 1))
        else:
            try:
                stack.extend((x, depth + 1) for x in list(vars(v).values()))
            except TypeError:
                for cls in type(v).__mro__:
                    slots = getattr(cls, "__slots__", ())
                    for slot in ((slots,) if isinstance(slots, str) else slots):
                        if hasattr(v, slot):
                            stack.append((getattr(v, slot), depth + 1))
    return out


def _strings(args) -> List[str]:
    """String-like arguments of an audit event (paths, hosts, command lines)."""
    out: List[str] = []

    def walk(a, depth=0):
        if depth > 4:
            return
        if isinstance(a, str):
            out.append(a)
        elif isinstance(a, bytes):
            try:
                out.append(a.decode("utf-8"))
            except UnicodeDecodeError:
                pass
        elif isinstance(a, os.PathLike):
            try:
                walk(os.fspath(a), depth + 1)
            except Exception:
                pass
        elif isinstance(a, (list, tuple)):
            for x in a:
                walk(x, depth + 1)
    walk(args)
    return out


def _word_in(t: str, s: str) -> bool:
    """t occurs in s as a whole token (not inside a longer run of letters or digits)."""
    pre = r"(?<![A-Za-z0-9_])" if (t[0].isalnum() or t[0] == "_") else ""
    post = r"(?![A-Za-z0-9_])" if (t[-1].isalnum() or t[-1] == "_") else ""
    return re.search(pre + re.escape(t) + post, s) is not None


class TaintGuard:
    """Decides, per audit event, whether a protected operation is healing-influenced.

    events      the audit events that are policed (default: protocol.PROTECTED_EVENTS; give
                another set to model a per-repository list as in HealGuard)
    inherit     tainted values from earlier repairs of the same run: a value a committed
                repair introduced stays tainted for the rest of the run (lineage)
    A guard is in phase "healing" while the repair runs and "continuation" afterwards.
    `truncated` is True when the state walk hit a limit (coverage is partial).
    """

    def __init__(self, min_len: int = 4, safe_paths: Iterable[str] = (),
                 events: Optional[Iterable[str]] = None, inherit: Iterable[str] = ()):
        self.min_len = min_len
        self.safe_paths = tuple(safe_paths)       # path prefixes the repair may write to
        self.events = frozenset(PROTECTED_EVENTS if events is None else events)
        self.phase = "idle"
        self.before: Set[str] = set()
        self.tainted: Set[str] = set(inherit)
        self.truncated = False

    def start(self, roots: Iterable) -> None:
        """Before the healing code runs: remember what the state already contains."""
        stats: dict = {}
        self.before = leaves(roots, self.min_len, stats)
        self.truncated = bool(stats.get("truncated"))
        self.phase = "healing"

    def finish(self, roots: Iterable, *new_bindings: dict) -> None:
        """After the healing code: collect the values it introduced."""
        stats: dict = {}
        after = leaves(list(roots) + [b for b in new_bindings if b], self.min_len, stats)
        self.truncated = self.truncated or bool(stats.get("truncated"))
        self.tainted |= after - self.before
        self.phase = "continuation"

    def __call__(self, event: str, args) -> Optional[str]:
        """Reason (str) if this protected operation is healing-influenced, else None."""
        strings = _strings(args)
        if self.safe_paths and strings and any(strings[0].startswith(p) for p in self.safe_paths):
            return None
        if self.phase == "healing":
            return "performed by the healing code itself"
        if self.phase != "continuation" or not self.tainted:
            return None
        for s in strings:
            if s in self.tainted:
                return f"argument {s[:80]!r} was introduced by the healing code"
            for part in s.replace("\\", "/").split("/"):
                if len(part) >= _COMPONENT_MIN and part in self.tainted:
                    return f"path component {part[:80]!r} was introduced by the healing code"
            if len(self.tainted) <= _SUBSTRING_LIMIT:
                for t in self.tainted:
                    if len(t) >= _SUBSTRING_MIN and _word_in(t, s):
                        return f"argument contains {t[:80]!r}, introduced by the healing code"
        return None