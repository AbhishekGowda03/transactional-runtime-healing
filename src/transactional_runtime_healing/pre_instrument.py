"""Pre-instrumentation: instrument a file with a newer Python, load the result on Python 3.7.

The instrumenter (instrument.py) needs `ast.unparse` (3.9) and end positions (3.8), and a code
object made by one Python version cannot be loaded by another. So for Python 3.7 the rewrite is
done ahead of time by a newer Python, and what is stored is the rewritten SYNTAX TREE (with the
line numbers of the original file), not source text and not bytecode:

    host (Python 3.9 or newer)                       Python 3.7 (the repository's interpreter)
    ---------------------------------                ------------------------------------------
    instrument_tree(source)  -> tree, table, ...     read the file  ->  tree_from_data(...)
    to_data(...)  -> JSON, zlib   ---- cache dir --> compile(tree, filename, "exec")

The file name of an entry is derived from the file's content and its path, in the same way as
repo_hook does for its own cache, so the loader on 3.7 finds the entry of the file it is about to
import (`entry_name`). A file without an entry is loaded unchanged and listed as skipped.

Command line (on the host, Python 3.9 to 3.12):

    python -m transactional_runtime_healing.pre_instrument --root DIR --as-root /testbed --cache CACHE

instruments every wanted .py file under DIR as if it lived at --as-root (the path inside the
container) and writes the entries to CACHE. `experiments/healbench/preinstrument_image.py` does this
for a Docker image.

The decoding side (tree_from_data, load_entry) must run on Python 3.7: no syntax newer than 3.7.
"""
import ast
import base64
import hashlib
import json
import os
import sys
import zlib

FORMAT = 1
_LEGACY_AST = sys.version_info < (3, 8)        # the parser still makes Num/Str/NameConstant/...
_OLD_SLICES = sys.version_info < (3, 9)        # Subscript.slice is wrapped in Index/ExtSlice


# ----------------------------------------------------------------------
# names
# ----------------------------------------------------------------------
def entry_name(raw: bytes, filename: str, version: str) -> str:
    """Name of the cache entry of a file (the same key as repo_hook's own cache)."""
    return hashlib.sha1(raw + version.encode() + filename.encode()).hexdigest() + ".ast.z"


# ----------------------------------------------------------------------
# encoding (host)
# ----------------------------------------------------------------------
class _Encoder:
    def __init__(self):
        self.classes = []                        # [name, [field names], has location]
        self.index = {}

    def cid(self, node):
        cls = type(node)
        if cls not in self.index:
            self.index[cls] = len(self.classes)
            self.classes.append([cls.__name__, list(cls._fields),
                                 "lineno" in cls._attributes])
        return self.index[cls]

    def value(self, v):
        if v is None or isinstance(v, (bool, int, str)):
            return v
        if isinstance(v, ast.AST):
            node = {"c": self.cid(v), "f": [self.value(getattr(v, f, None)) for f in v._fields]}
            if "lineno" in v._attributes:
                node["l"] = getattr(v, "lineno", None)
                node["o"] = getattr(v, "col_offset", None)
            return node
        if isinstance(v, list):
            return [self.value(x) for x in v]
        if isinstance(v, float):
            return {"fl": repr(v)}
        if isinstance(v, complex):
            return {"cx": [repr(v.real), repr(v.imag)]}
        if isinstance(v, bytes):
            return {"by": base64.b64encode(v).decode("ascii")}
        if v is Ellipsis:
            return {"el": 1}
        if isinstance(v, tuple):
            return {"tp": [self.value(x) for x in v]}
        if isinstance(v, frozenset):
            return {"fs": [self.value(x) for x in sorted(v, key=repr)]}
        raise TypeError("cannot store a %s in a syntax tree entry" % type(v).__name__)


def to_data(tree, table, reads, spans, filename):
    """The entry (a dict that json can store) for an instrumented tree."""
    enc = _Encoder()
    body = enc.value(tree)
    return {"format": FORMAT, "python": "%d.%d.%d" % sys.version_info[:3], "filename": filename,
            "classes": enc.classes, "tree": body,
            "table": [[a, b, list(t)] for a, b, t in table],
            "reads": sorted(reads), "spans": [list(s) for s in spans]}


def dumps(data) -> bytes:
    return zlib.compress(json.dumps(data, separators=(",", ":")).encode("utf-8"), 6)


# ----------------------------------------------------------------------
# decoding (Python 3.7 and newer)
# ----------------------------------------------------------------------
def _legacy_constant(value):
    """Python 3.7's parser makes Str/Num/Bytes/NameConstant/Ellipsis, not Constant."""
    if isinstance(value, bool) or value is None:
        return ast.NameConstant(value=value)
    if isinstance(value, str):
        return ast.Str(s=value)
    if isinstance(value, bytes):
        return ast.Bytes(s=value)
    if isinstance(value, (int, float, complex)):
        return ast.Num(n=value)
    if value is Ellipsis:
        return ast.Ellipsis()
    return ast.Constant(value=value)


def _old_slice(node):
    """Python 3.9 stores a subscript's index as an ordinary expression, 3.7 wraps it."""
    if isinstance(node, (ast.Slice, ast.ExtSlice, ast.Index)):
        return node
    if isinstance(node, ast.Tuple) and any(isinstance(e, ast.Slice) for e in node.elts):
        return ast.ExtSlice(dims=[e if isinstance(e, ast.Slice) else ast.Index(value=e)
                                  for e in node.elts])
    return ast.Index(value=node)


class _Decoder:
    def __init__(self, classes):
        self.classes = []
        for name, fields, located in classes:
            cls = getattr(ast, name, None)
            if cls is None:
                raise ValueError("this Python has no ast.%s" % name)
            own = set(cls._fields)
            keep = [i for i, f in enumerate(fields) if f in own]
            self.classes.append((cls, keep, [fields[i] for i in keep], "lineno" in cls._attributes,
                                 _OLD_SLICES and cls is ast.Subscript,
                                 _LEGACY_AST and cls is ast.Constant))

    def value(self, v):
        t = type(v)
        if t is dict:
            if "c" in v:
                cls, keep, names, located, subscript, constant = self.classes[v["c"]]
                values = v["f"]
                node = cls()
                d = node.__dict__
                value = self.value
                for i, name in zip(keep, names):
                    x = values[i]
                    tx = type(x)
                    d[name] = x if (x is None or tx is str or tx is int or tx is bool) else value(x)
                if located and "l" in v:
                    d["lineno"], d["col_offset"] = v["l"], v["o"]
                if subscript:
                    node.slice = _old_slice(node.slice)
                if constant:
                    new = _legacy_constant(node.value)
                    if "l" in v:
                        new.lineno, new.col_offset = v["l"], v["o"]
                    return new
                return node
            if "fl" in v:
                return float(v["fl"])
            if "cx" in v:
                return complex(float(v["cx"][0]), float(v["cx"][1]))
            if "by" in v:
                return base64.b64decode(v["by"])
            if "el" in v:
                return Ellipsis
            if "tp" in v:
                return tuple(self.value(x) for x in v["tp"])
            if "fs" in v:
                return frozenset(self.value(x) for x in v["fs"])
            raise ValueError("unknown entry value")
        if t is list:
            value = self.value
            return [x if (x is None or type(x) is str or type(x) is int) else value(x) for x in v]
        return v


def tree_from_data(data):
    if data.get("format") != FORMAT:
        raise ValueError("entry format %r, expected %r" % (data.get("format"), FORMAT))
    return _Decoder(data["classes"]).value(data["tree"])


def loads(blob: bytes):
    return json.loads(zlib.decompress(blob).decode("utf-8"))


def _compiled_path(path):
    """Where the running Python keeps the code object it made from an entry (decoding a big
    entry takes seconds, so it is done once per Python version)."""
    import importlib.util
    tag = "py%d%d-%s" % (sys.version_info[0], sys.version_info[1],
                         importlib.util.MAGIC_NUMBER.hex())
    return "%s.%s.code" % (path[:-len(".ast.z")] if path.endswith(".ast.z") else path, tag)


def load_entry(path, use_cache=True):
    """(code object, handler table, names read, function spans) of a stored entry; compiled by
    the Python that is running (this is the part that runs on 3.7). The result is kept next to the
    entry as a marshal file of this Python version and read from there the next time."""
    import marshal
    cpath = _compiled_path(path)
    if use_cache and os.path.exists(cpath):
        try:
            with open(cpath, "rb") as f:
                code, table, reads, spans = marshal.loads(f.read())
            return code, [(a, b, list(t)) for a, b, t in table], set(reads), [tuple(x) for x in spans]
        except Exception:
            pass                                  # damaged or from another build: decode again
    with open(path, "rb") as f:
        data = loads(f.read())
    tree = tree_from_data(data)
    filename = data["filename"]
    code = compile(tree, filename, "exec")
    table = [(a, b, list(t)) for a, b, t in data["table"]]
    reads, spans = set(data["reads"]), [tuple(x) for x in data["spans"]]
    if use_cache:
        try:
            tmp = "%s.%d.tmp" % (cpath, os.getpid())
            with open(tmp, "wb") as f:
                f.write(marshal.dumps((code, [(a, b, tuple(t)) for a, b, t in table],
                                       sorted(reads), spans)))
            os.replace(tmp, cpath)
        except OSError:
            pass                                  # a read-only cache folder: works, only slower
    return code, table, reads, spans


def warm(cache_dir, jobs=1):
    """Decode every entry in `cache_dir` with the running Python and keep the code objects. Run
    this with the repository's own interpreter (in the container) before timing anything."""
    names = sorted(os.path.join(cache_dir, n) for n in os.listdir(cache_dir) if n.endswith(".ast.z"))
    todo = [n for n in names if not os.path.exists(_compiled_path(n))]
    failed = 0
    for n in todo:
        try:
            load_entry(n)
        except Exception:
            failed += 1
    return len(names), len(todo), failed


# ----------------------------------------------------------------------
# writing entries (host)
# ----------------------------------------------------------------------
def build_entry(raw: bytes, filename: str):
    """Instrument one file; the bytes of the entry. Raises SyntaxError and friends when the file
    cannot be rewritten (also when it is not valid Python 3.7)."""
    import warnings
    from transactional_runtime_healing import instrument as ins
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # feature_version: a file that Python 3.7 could not parse is not instrumented either (only
        # enforced fully by a host from 3.10 on; on 3.9 such a file gets an entry that 3.7 then
        # cannot compile, and it is loaded unchanged)
        tree, table, reads, spans = ins.instrument_tree(raw.decode("utf-8"), filename,
                                                        feature_version=(3, 7))
        compile(tree, filename, "exec")           # the same errors the direct loader reports
    return dumps(to_data(tree, table, reads, spans, filename))


def write_entry(raw: bytes, filename: str, cache_dir: str, version: str):
    """Instrument `raw` (the content of `filename`) into `cache_dir`. Returns the entry's name;
    skips work when it exists."""
    name = entry_name(raw, filename, version)
    path = os.path.join(cache_dir, name)
    if os.path.exists(path):
        return name
    blob = build_entry(raw, filename)
    os.makedirs(cache_dir, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, path)
    return name


def _one(job):
    real, filename, cache_dir, version = job
    try:
        with open(real, "rb") as f:
            raw = f.read()
        write_entry(raw, filename, cache_dir, version)
        return filename, None
    except (SyntaxError, UnicodeDecodeError, RecursionError, ValueError, OSError) as e:
        return filename, "%s: %s" % (type(e).__name__, str(e)[:100])


def wanted_files(root, as_root):
    """[(real path, path at run time)] of the files the loader would instrument."""
    from transactional_runtime_healing import repo_hook
    out = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d.lower() not in repo_hook.EXCLUDE_DIRS]
        for name in files:
            if name.endswith(".py"):
                real = os.path.join(dirpath, name)
                filename = os.path.join(as_root, os.path.relpath(real, root))
                if repo_hook.wanted(filename, as_root):
                    out.append((real, filename))
    return out


def instrument_tree_of(root, as_root, cache_dir, version, jobs=1):
    """Instrument every wanted file under `root` as if the tree lived at `as_root`. Returns
    (written, failed) where failed is [(file, error)]."""
    jobs_list = [(r, f, cache_dir, version) for r, f in wanted_files(root, as_root)]
    if jobs > 1 and len(jobs_list) > 1:
        import multiprocessing
        with multiprocessing.Pool(jobs) as pool:
            results = list(pool.imap_unordered(_one, jobs_list, chunksize=8))
    else:
        results = [_one(j) for j in jobs_list]
    failed = [(f, e) for f, e in results if e]
    return len(results) - len(failed), failed


def main(argv=None):
    import argparse
    from transactional_runtime_healing import repo_hook
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--warm", action="store_true",
                    help="decode the entries of --cache with this Python (run in the container)")
    ap.add_argument("--root", help="directory with the repository's files")
    ap.add_argument("--as-root", default="/testbed", help="where the files live at run time")
    ap.add_argument("--cache", required=True, help="cache directory (mounted as /hookcache)")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    a = ap.parse_args(argv)
    if a.warm:
        total, done, bad = warm(a.cache)
        print("warmed %d of %d entries on Python %d.%d, %d failed" % (
            done, total, sys.version_info[0], sys.version_info[1], bad))
        return 0
    if not a.root:
        ap.error("--root is required")
    written, failed = instrument_tree_of(os.path.abspath(a.root), a.as_root, a.cache,
                                         repo_hook._VERSION, a.jobs)
    print("pre-instrumented %d files, %d could not be rewritten" % (written, len(failed)))
    for f, e in failed[:20]:
        print("  ", f, e)
    return 0


if __name__ == "__main__":
    sys.exit(main())