"""Instrumentation: catch real exceptions and call a healer at the crash point.

The source of a program is rewritten so that statements are wrapped in
try/except, in the way HealBench does it (Fig. 3 of the HealBench paper):

    try:
        result = compute(x)
    except Exception as __eh_e:
        <ask the healer; re-raise if it does not heal; write bindings back>

* Simple and compound statements are wrapped. Function and class definitions
  are not wrapped, but the statements inside functions are. Class bodies are not
  instrumented (their methods are).
* `return expr` is evaluated into `_eh_rtn` first, so a failure in the return
  expression can be healed; the healing code can set `_eh_rtn` to supply the
  return value.
* After a successful healing, execution continues after the wrapped statement;
  the failed statement is not retried. For a compound statement this skips its
  unfinished work.
* `raise`, `assert`, `pass`, `break`, `continue`, `global`, `nonlocal`,
  docstrings and `from __future__` imports are never wrapped.
* A statement inside a `try:` body is not wrapped for the exceptions that
  try's handlers catch (the program expects them). If a handler catches
  everything (bare `except:`, `Exception`, `BaseException`) the statement is not
  wrapped at all.
* Bindings returned by the healer are written back to the frame: names that are
  local to the function are assigned; other names go to the module globals.
  Names the function never assigns cannot become locals, so they go to globals.

The healer decides what happens. `Healer` below calls `heal_point` (trial in a
forked child, commit by replay) with healing code given by a `propose`
function (an LLM in the real system).

Limits: expression-level constructs (comprehensions, lambdas) are not wrapped on
their own; a crash inside them is caught by the statement around them. Only
`Exception` is caught.
"""
import ast
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from transactional_runtime_healing.heal_point import HealResult, heal_point

_NO_WRAP = (ast.Pass, ast.Break, ast.Continue, ast.Global, ast.Nonlocal,
            ast.Raise, ast.Assert, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_CATCH_ALL = {"Exception", "BaseException"}
_SOURCE_LIMIT = 4000


# ----------------------------------------------------------------------
# Names that are local to a function (these can be assigned by the handler)
# ----------------------------------------------------------------------
def _local_names(fn) -> List[str]:
    names = set()
    a = fn.args
    for arg in a.posonlyargs + a.args + a.kwonlyargs:
        names.add(arg.arg)
    for arg in (a.vararg, a.kwarg):
        if arg is not None:
            names.add(arg.arg)

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(child.name)
                continue                                    # own scope
            if isinstance(child, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp,
                                  ast.GeneratorExp)):
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
                names.add(child.id)
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                for alias in child.names:
                    names.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(child, ast.ExceptHandler) and child.name:
                names.add(child.name)
            walk(child)

    for stmt in fn.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(stmt.name)
        else:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                for alias in stmt.names:
                    names.add((alias.asname or alias.name).split(".")[0])
            walk(ast.Module(body=[stmt], type_ignores=[]))
    names.add("_eh_rtn")
    # names starting with "__" would be mangled inside classes; the handler's own names
    return sorted(n for n in names if not n.startswith("__"))


def _is_docstring(stmt) -> bool:
    return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str))


# ----------------------------------------------------------------------
class _Instrumenter:
    def __init__(self, source: str, filename: str):
        self.source = source
        self.filename = filename

    # -- public ----------------------------------------------------------
    def module(self, tree: ast.Module) -> ast.Module:
        tree.body = self.block(tree.body, None, [], "<module>")
        return ast.fix_missing_locations(tree)

    # -- blocks ----------------------------------------------------------
    def block(self, stmts, names, expected, func):
        out = []
        for stmt in stmts:
            out.extend(self.statement(stmt, names, expected, func))
        return out

    def statement(self, stmt, names, expected, func):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stmt.body = self.block(stmt.body, _local_names(stmt), [], stmt.name)
            return [stmt]
        if isinstance(stmt, ast.ClassDef):                  # only the methods
            for i, item in enumerate(stmt.body):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    stmt.body[i] = self.statement(item, None, [], func)[0]
            return [stmt]

        self.recurse(stmt, names, expected, func)

        if isinstance(stmt, _NO_WRAP) or _is_docstring(stmt):
            return [stmt]
        if isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__":
            return [stmt]
        if isinstance(stmt, ast.Match):
            return [stmt]
        if isinstance(stmt, ast.Return) and stmt.value is None:
            return [stmt]
        if any(e is None for e in expected):               # a handler catches everything
            return [stmt]

        if isinstance(stmt, ast.Return):
            return self.wrap_return(stmt, names, expected, func)
        return [self.wrap(stmt, names, expected, func)]

    def recurse(self, stmt, names, expected, func):
        """Instrument the statements nested inside a compound statement."""
        if isinstance(stmt, ast.Try) or stmt.__class__.__name__ == "TryStar":
            inner = list(expected)
            for h in stmt.handlers:
                inner.append(self._handler_types(h))
            stmt.body = self.block(stmt.body, names, inner, func)
            for h in stmt.handlers:
                h.body = self.block(h.body, names, expected, func)
            stmt.orelse = self.block(stmt.orelse, names, expected, func)
            stmt.finalbody = self.block(stmt.finalbody, names, expected, func)
            return
        if isinstance(stmt, ast.Match):
            for case in stmt.cases:
                case.body = self.block(case.body, names, expected, func)
            return
        for attr in ("body", "orelse", "finalbody"):
            if hasattr(stmt, attr) and isinstance(getattr(stmt, attr), list):
                setattr(stmt, attr, self.block(getattr(stmt, attr), names, expected, func))

    @staticmethod
    def _handler_types(h):
        """Source of the exception types a handler catches; None = catches everything."""
        if h.type is None:
            return None
        elts = h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]
        for e in elts:
            if isinstance(e, ast.Name) and e.id in _CATCH_ALL:
                return None
        return ast.unparse(h.type)

    # -- wrapping --------------------------------------------------------
    def handler_body(self, stmt, names, expected, func, lineno):
        segment = ast.get_source_segment(self.source, stmt) or ""
        site = (self.filename, lineno, func, segment[:_SOURCE_LIMIT])
        lines = []
        types = [e for e in expected if e]
        if types:
            lines.append(f"if isinstance(__eh_e, ({', '.join(types)},)): raise")
        loc = "dict(locals())" if names is not None else "{}"
        lines.append(f"__eh_r = __heal__(__eh_e, {loc}, globals(), {site!r})")
        lines.append("if __eh_r is None: raise")
        for n in (names or []):
            lines.append(f"if {n!r} in __eh_r.bindings: {n} = __eh_r.bindings[{n!r}]")
        lines.append(f"__heal_apply__(__eh_r, globals(), {tuple(names or [])!r})")
        return ast.parse("\n".join(lines)).body

    def wrap(self, stmt, names, expected, func):
        node = ast.Try(
            body=[stmt],
            handlers=[ast.ExceptHandler(type=ast.Name(id="Exception", ctx=ast.Load()),
                                        name="__eh_e",
                                        body=self.handler_body(stmt, names, expected, func,
                                                               stmt.lineno))],
            orelse=[], finalbody=[])
        return ast.copy_location(node, stmt)

    def wrap_return(self, stmt, names, expected, func):
        init = ast.parse("_eh_rtn = None").body[0]
        assign = ast.Assign(targets=[ast.Name(id="_eh_rtn", ctx=ast.Store())], value=stmt.value)
        ast.copy_location(assign, stmt)
        ret = ast.Return(value=ast.Name(id="_eh_rtn", ctx=ast.Load()))
        for n in (init, ret):
            ast.copy_location(n, stmt)
        return [init, self.wrap(assign, names, expected, func), ret]


# ----------------------------------------------------------------------
def instrument(source: str, filename: str = "<instrumented>"):
    """Return the code object of `source` with heal points inserted."""
    tree = ast.parse(source, filename)
    tree = _Instrumenter(source, filename).module(tree)
    return compile(tree, filename, "exec")


def instrumented_source(source: str, filename: str = "<instrumented>") -> str:
    """The rewritten source as text (for inspection and for the thesis)."""
    tree = ast.parse(source, filename)
    return ast.unparse(_Instrumenter(source, filename).module(tree))


def _apply(result: HealResult, globals_: dict, local_names: tuple):
    for k, v in result.bindings.items():
        if k not in local_names:
            globals_[k] = v
    globals_.update(result.globals_updates)


def run_instrumented(source: str, healer: Callable, filename: str = "<instrumented>",
                     globals_: Optional[dict] = None) -> dict:
    """Run `source` with heal points; returns the module's globals afterwards."""
    g = {"__name__": "__main__"} if globals_ is None else globals_
    g["__heal__"] = healer
    g["__heal_apply__"] = _apply
    exec(instrument(source, filename), g)
    return g


# ----------------------------------------------------------------------
@dataclass
class HealContext:
    """What the healer (and the LLM) is told about the crash."""
    exception: BaseException
    filename: str
    lineno: int
    function: str
    statement: str
    locals_: Dict[str, Any]
    globals_: Dict[str, Any]


@dataclass
class HealRecord:
    context: HealContext
    code: Optional[str]
    result: Optional[HealResult]


class Healer:
    """Healer that calls `heal_point` with code from `propose(context)`.

    propose(context) -> healing code (str) or None (do not heal: the original
    exception propagates). `checks` as in heal_point. `max_heals` bounds the
    number of heal attempts per run (a safety budget). `log` keeps one record per
    crash, for the evaluation (proceed rate, rejections, divergence).
    """

    def __init__(self, propose: Callable[[HealContext], Optional[str]],
                 checks: Optional[list] = None, max_heals: int = 50, **heal_point_kw):
        self.propose = propose
        self.checks = checks or []
        self.max_heals = max_heals
        self.kw = heal_point_kw
        self.log: List[HealRecord] = []
        self._busy = False

    def __call__(self, exc, locals_, globals_, site):
        if self._busy or len(self.log) >= self.max_heals:     # no nested healing
            return None
        filename, lineno, func, statement = site
        clean = {k: v for k, v in locals_.items() if not k.startswith("__eh")}
        ctx = HealContext(exc, filename, lineno, func, statement, clean, globals_)
        self._busy = True
        try:
            code = self.propose(ctx)
            if code is None:
                self.log.append(HealRecord(ctx, None, None))
                return None
            result = heal_point(clean, globals_, code, self.checks, **self.kw)
            self.log.append(HealRecord(ctx, code, result))
            return result if result.healed else None
        finally:
            self._busy = False