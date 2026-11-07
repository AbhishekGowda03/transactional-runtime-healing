"""Instrument a whole repository as its modules are imported (HealBench-style).

    from transactional_runtime_healing import repo_hook
    repo_hook.install("/path/to/repo", healer)        # before the repository is imported

Every module under `root` is rewritten with heal points (instrument.py) when it is imported,
except what HealBench also leaves alone: tests, docs, examples, benchmarks, vendored and
third-party code (see EXCLUDE_DIRS / EXCLUDE_FILES). Modules imported before install() are not
instrumented. The healer is placed in each module's globals as `__heal__` and `__heal_apply__`.

Rewriting a big library takes time (sympy: tens of seconds), so the instrumented code is cached
on disk, keyed by the file's content and the instrumenter's version. Limits: only .py files with
a source loader; compiled extensions and zip imports are not touched; the exceptions a test
expects (`pytest.raises`) are not known to the healer, so run the tests that must go from
failing to passing, not whole test suites.
"""
import hashlib
import importlib.abc
import importlib.machinery
import marshal
import os
import pickle
import sys

from transactional_runtime_healing import instrument as _ins

EXCLUDE_DIRS = {"tests", "test", "testing", "docs", "doc", "examples", "example", "benchmarks",
                "benchmark", "build", "dist", "vendor", "_vendor", "vendored", "third_party",
                "site-packages", "node_modules", ".git", "__pycache__", "scripts", "bin"}
EXCLUDE_FILES = {"conftest.py", "setup.py"}
_VERSION = "4"                                    # change when the instrumenter changes


def wanted(path: str, root: str) -> bool:
    """Is this file part of the repository's own code that should get heal points?"""
    p = os.path.realpath(path)
    r = os.path.realpath(root)
    if not p.startswith(r + os.sep) or not p.endswith(".py"):
        return False
    parts = os.path.relpath(p, r).split(os.sep)
    if any(d in EXCLUDE_DIRS for d in parts[:-1]):
        return False
    name = parts[-1]
    return not (name in EXCLUDE_FILES or name.startswith("test_") or name.endswith("_test.py"))


class _HealLoader(importlib.abc.Loader):
    def __init__(self, inner, healer, cache_dir, skipped):
        self.inner, self.healer, self.cache_dir, self.skipped = inner, healer, cache_dir, skipped

    def __getattr__(self, name):                  # get_filename, is_package, get_data, ...
        return getattr(self.inner, name)

    def create_module(self, spec):
        return self.inner.create_module(spec) if hasattr(self.inner, "create_module") else None

    def _code(self, filename):
        with open(filename, "rb") as f:
            raw = f.read()
        key = hashlib.sha1(raw + _VERSION.encode() + filename.encode()).hexdigest()
        path = os.path.join(self.cache_dir, key + ".pkl") if self.cache_dir else None
        if path and os.path.exists(path + ".fail"):      # known not to be rewritable
            raise SyntaxError("not instrumentable (cached)")
        if path and os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    blob, table, reads = pickle.load(f)
                _ins.HANDLER_TABLES[filename] = table
                _ins.READ_NAMES[filename] = reads
                return marshal.loads(blob)
            except Exception:
                pass                              # a damaged cache entry: rebuild it
        try:
            code, table = _ins.instrument_with_table(raw.decode("utf-8"), filename)
        except (SyntaxError, UnicodeDecodeError, RecursionError) as e:
            if path:
                os.makedirs(self.cache_dir, exist_ok=True)
                with open(path + ".fail", "w") as f:
                    f.write(f"{type(e).__name__}: {e}\n{filename}\n")
            raise
        if path:
            os.makedirs(self.cache_dir, exist_ok=True)
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "wb") as f:
                pickle.dump((marshal.dumps(code), table, _ins.READ_NAMES[filename]), f)
            os.replace(tmp, path)
        return code

    def exec_module(self, module):
        filename = module.__spec__.origin
        try:
            code = self._code(filename)
        except (SyntaxError, UnicodeDecodeError, RecursionError):
            self.skipped.append(filename)             # cannot be rewritten: load it unchanged
            return self.inner.exec_module(module)
        module.__dict__["__heal__"] = self.healer
        module.__dict__["__heal_apply__"] = _ins._apply
        exec(code, module.__dict__)


class HealingFinder(importlib.abc.MetaPathFinder):
    def __init__(self, root, healer, cache_dir=None):
        self.root, self.healer, self.cache_dir = root, healer, cache_dir
        self.skipped = []                         # files that could not be rewritten
        self.instrumented = []                    # files that were loaded with heal points

    def find_spec(self, fullname, path=None, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None or not spec.origin or spec.loader is None:
            return None
        if not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            return None
        if not wanted(spec.origin, self.root):
            return None
        spec.loader = _HealLoader(spec.loader, self.healer, self.cache_dir, self.skipped)
        self.instrumented.append(spec.origin)
        return spec


def install(root, healer, cache_dir=None) -> HealingFinder:
    finder = HealingFinder(root, healer, cache_dir)
    sys.meta_path.insert(0, finder)
    return finder


def uninstall(finder):
    if finder in sys.meta_path:
        sys.meta_path.remove(finder)