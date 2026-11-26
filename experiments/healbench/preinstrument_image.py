"""Pre-instrument the repository of a task image for a Python 3.7 run.

The instrumenter needs Python 3.9; the repository inside an old image runs on 3.7 (or 3.8). So the
rewrite is done here on the host: the repository's files are copied out of the image, rewritten
(pre_instrument.py) as if they lived at /testbed, and the result goes to the hook cache that the
container mounts as /hookcache. The loader on 3.7 then compiles the stored syntax trees.

    uv run python experiments/healbench/preinstrument_image.py IMAGE --cache ~/r2e_work/hookcache_numpy
    uv run python experiments/healbench/preinstrument_image.py IMAGE --cache DIR --check

From code:  ensure(image, cache_dir) -> dict (python, files, failed, seconds); it returns at once when
the image's Python is 3.9 or newer, or when this image was done before (marker file in the cache).
The host must run Python 3.9 or newer (3.12 is fine) and have src/ on the path (this script adds it).
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

_PYTHON = {}


def python_of(image, interpreter="/testbed/.venv/bin/python"):
    """(major, minor) of the repository's interpreter in the image; pulls the image if needed."""
    if image not in _PYTHON:
        p = subprocess.run(["docker", "run", "--rm", image, interpreter, "-c",
                            "import sys; print(sys.version_info[0], sys.version_info[1])"],
                           capture_output=True, text=True, timeout=1800)
        major, minor = p.stdout.split()[-2:]
        _PYTHON[image] = (int(major), int(minor))
    return _PYTHON[image]


def copy_repository(image, dest, timeout=900):
    """The repository's .py files, streamed out of the image with tar (the virtual environment and
    .git are left out)."""
    inner = ("cd /testbed && find . -name '*.py' -not -path './.venv/*' -not -path './.git/*' "
             "-not -path './node_modules/*' -print0 | tar --null -T - -cf -")
    src = subprocess.Popen(["docker", "run", "--rm", image, "bash", "-c", inner],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    dst = subprocess.Popen(["tar", "-xf", "-", "-C", dest], stdin=src.stdout)
    src.stdout.close()
    dst.wait(timeout=timeout)
    err = src.stderr.read().decode(errors="replace")
    src.wait(timeout=timeout)
    if src.returncode not in (0, None) or dst.returncode != 0:
        raise RuntimeError("could not copy the repository out of %s: %s" % (image, err[-300:]))


def marker_path(image, cache_dir):
    return os.path.join(cache_dir, "pre_%s.json" % hashlib.sha1(image.encode()).hexdigest()[:16])


def warm(image, cache_dir, timeout=3600):
    """Decode the entries with the image's own Python (compiled code objects are kept next to them),
    so that the first healing run does not pay for it. Returns the tool's last output line."""
    script = ("/testbed/.venv/bin/python -m transactional_runtime_healing.pre_instrument "
              "--warm --cache /hookcache; rc=$?; chown -R %d:%d /hookcache 2>/dev/null; exit $rc"
              % (os.getuid(), os.getgid()))
    p = subprocess.run(["docker", "run", "--rm", "-e", "PYTHONPATH=/txh", "-e", "PYTHONDONTWRITEBYTECODE=1",
                        "-v", "%s:/txh:ro" % os.path.join(ROOT, "src"), "-v", "%s:/hookcache" % cache_dir,
                        image, "bash", "-c", script], capture_output=True, text=True, timeout=timeout)
    return (p.stdout.strip().splitlines() or [p.stderr.strip()[-200:]])[-1]


def ensure(image, cache_dir, jobs=None, force=False, do_warm=True):
    """Make sure the pre-instrumented entries of this image's repository are in `cache_dir`.
    Returns a dict for the result file: python, needed, files, failed, seconds."""
    py = python_of(image)
    info = {"image": image, "python": "%d.%d" % py, "needed": py < (3, 9)}
    if not info["needed"]:
        return info
    marker = marker_path(image, cache_dir)
    if os.path.exists(marker) and not force:
        with open(marker) as f:
            return json.load(f)
    from transactional_runtime_healing import pre_instrument, repo_hook
    t0 = time.monotonic()
    os.makedirs(cache_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="txh-pre-") as tmp:
        copy_repository(image, tmp)
        written, failed = pre_instrument.instrument_tree_of(tmp, "/testbed", cache_dir,
                                                            repo_hook._VERSION,
                                                            jobs or (os.cpu_count() or 1))
    info.update(files=written, failed=len(failed), failed_examples=failed[:10],
                seconds=round(time.monotonic() - t0, 1), host_python="%d.%d" % sys.version_info[:2])
    if do_warm:
        t1 = time.monotonic()
        info["warm"] = warm(image, cache_dir)
        info["warm_seconds"] = round(time.monotonic() - t1, 1)
    with open(marker, "w") as f:
        json.dump(info, f)
    return info


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("image")
    ap.add_argument("--cache", required=True, help="the hook cache folder (mounted as /hookcache)")
    ap.add_argument("--jobs", type=int)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-warm", action="store_true", help="do not decode the entries in the container")
    ap.add_argument("--check", action="store_true", help="only say what is needed")
    a = ap.parse_args(argv)
    if sys.version_info < (3, 9):
        raise SystemExit("run this on the host with Python 3.9 or newer")
    cache = os.path.abspath(os.path.expanduser(a.cache))
    if a.check:
        print(json.dumps({"python": "%d.%d" % python_of(a.image)}))
        return 0
    print(json.dumps(ensure(a.image, cache, a.jobs, a.force, not a.no_warm), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())