"""Find out which SWE-bench instances end in a RUNTIME EXCEPTION (useful for healing) and which
only in a failed assertion. Pure-Python repositories only, no Docker.

    uv run --with datasets python experiments/swebench/classify.py --repo sympy/sympy --limit 10
    ... --ids sympy__sympy-11400,sympy__sympy-12171      choose instances
    ... --parquet lite.parquet                           read the dataset from a local file

For every instance: check out the buggy commit (base_commit), apply the test patch, run the
tests that must go from failing to passing (FAIL_TO_PASS), and record what each did:
  RUNTIME_EXCEPTION  a test failed with an exception that is not an AssertionError, raised in
                     the repository's own code (not in test code)
  EXCEPTION_IN_TEST  an exception, but raised in test code or a test helper
  ASSERTION          every failing test ended in an AssertionError (a wrong result)
  PASSES             the tests pass on the buggy code: not reproduced here
  NOT_RUN            a collection error, a timeout or no result for the tests
Results go to results/swebench/classify_<repo>.jsonl (one line per instance, appended; an
instance already in the file is skipped). Work happens in --workdir (default ~/swe_work): one
clone per repository and one virtual environment per Python version.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# Repository recipes: Python version and packages to install into the virtual environment.
# The repository itself is used through PYTHONPATH (no install), which works for pure Python.
RECIPES = {
    "sympy/sympy": dict(python="3.9", packages=["pytest==6.2.5", "mpmath==1.3.0"]),
}


def load_rows(parquet=None):
    if parquet:
        import pyarrow.parquet as pq
        return pq.read_table(parquet).to_pylist()
    from datasets import load_dataset
    return [dict(r) for r in load_dataset("princeton-nlp/SWE-bench_Lite", split="test")]


def _tests(value):
    return json.loads(value) if isinstance(value, str) else list(value or [])


def run(cmd, cwd=None, env=None, timeout=600, input_text=None):
    return subprocess.run(cmd, cwd=cwd, env=env, input=input_text, capture_output=True, text=True,
                          timeout=timeout)


def ensure_repo(workdir, repo):
    path = os.path.join(workdir, "repos", repo.replace("/", "__"))
    if not os.path.isdir(os.path.join(path, ".git")):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        r = run(["git", "clone", "--filter=blob:none", "--no-checkout",
                 f"https://github.com/{repo}.git", path], timeout=1800)
        if r.returncode:
            raise RuntimeError("git clone failed: " + r.stderr[-300:])
    return path


def ensure_venv(workdir, recipe):
    path = os.path.join(workdir, "venvs", f"py{recipe['python']}-" + "-".join(
        p.split("==")[0] for p in recipe["packages"]))
    py = os.path.join(path, "bin", "python")
    if not os.path.exists(py):
        uv = shutil.which("uv") or "uv"
        r = run([uv, "venv", "--python", recipe["python"], path], timeout=900)
        if r.returncode:
            raise RuntimeError("uv venv failed: " + r.stderr[-300:])
        r = run([uv, "pip", "install", "--python", py, *recipe["packages"]], timeout=900)
        if r.returncode:
            raise RuntimeError("package install failed: " + r.stderr[-300:])
    return py


def test_files(test_patch):
    return [line.split(" b/", 1)[1] for line in test_patch.splitlines()
            if line.startswith("diff --git") and " b/" in line]


def classify_instance(row, repo_dir, py, timeout):
    t0 = time.monotonic()
    out = dict(instance_id=row["instance_id"], repo=row["repo"], version=row["version"],
               python=run([py, "-c", "import sys;print(sys.version.split()[0])"]).stdout.strip())
    f2p = _tests(row["FAIL_TO_PASS"])
    out["fail_to_pass"] = f2p

    def git(*args, **kw):
        return run(["git", *args], cwd=repo_dir, **kw)
    git("reset", "--hard", "-q")
    git("clean", "-fdxq")
    r = git("checkout", "-q", "-f", row["base_commit"], timeout=1800)
    if r.returncode:
        return dict(out, status="NOT_RUN", detail="checkout failed: " + r.stderr[-200:])
    r = git("apply", "--whitespace=nowarn", "-", input_text=row["test_patch"])
    if r.returncode:
        return dict(out, status="NOT_RUN", detail="test patch does not apply: " + r.stderr[-200:])
    results = os.path.join(repo_dir, ".classify_results.jsonl")
    if os.path.exists(results):
        os.remove(results)
    env = dict(os.environ, CLASSIFY_OUT=results, CLASSIFY_ROOT=repo_dir,
               PYTHONPATH=os.pathsep.join([repo_dir, HERE]), PYTHONDONTWRITEBYTECODE="1")
    k = " or ".join(t.split("::")[-1].split("[")[0] for t in f2p)
    cmd = [py, "-m", "pytest", "-p", "classify_plugin", "-p", "no:cacheprovider", "-q", "-k", k, *test_files(row["test_patch"])]
    try:
        p = run(cmd, cwd=repo_dir, env=env, timeout=timeout)
        out["pytest_exit"] = p.returncode
        tail = (p.stdout + p.stderr)[-300:]
    except subprocess.TimeoutExpired:
        git("reset", "--hard", "-q")
        git("clean", "-fdxq")
        return dict(out, status="NOT_RUN", detail=f"timeout after {timeout}s")
    recs = []
    if os.path.exists(results):
        recs = [json.loads(x) for x in open(results).read().splitlines() if x.strip()]
        os.remove(results)
    git("reset", "--hard", "-q")
    git("clean", "-fdxq")
    wanted = {t.split("::")[-1].split("[")[0] for t in f2p}
    mine = [r for r in recs if r["name"].split("[")[0] in wanted]
    out["tests"] = [{k2: r.get(k2) for k2 in ("name", "outcome", "exc_type", "exc_msg", "raised_at",
                                              "raised_in_repo_code", "repo_frames")} for r in mine]
    failed = [r for r in mine if r["outcome"] == "failed"]
    non_assert = [r for r in failed if r.get("exc_type") not in (None, "AssertionError")]
    if not mine:
        out.update(status="NOT_RUN", detail="no result for the tests: " + tail.replace("\n", " | "))
    elif not failed:
        out["status"] = "PASSES"
    elif any(r.get("raised_in_repo_code") for r in non_assert):
        out["status"] = "RUNTIME_EXCEPTION"
    elif non_assert:
        out["status"] = "EXCEPTION_IN_TEST"
    else:
        out["status"] = "ASSERTION"
    out["seconds"] = round(time.monotonic() - t0, 1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="sympy/sympy")
    ap.add_argument("--ids", help="comma list of instance ids")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--parquet", help="read the dataset from this local file")
    ap.add_argument("--workdir", default=os.path.expanduser("~/swe_work"))
    ap.add_argument("--timeout", type=int, default=600, help="seconds per instance")
    ap.add_argument("--out-dir", default=os.path.join("results", "swebench"))
    ns = ap.parse_args()
    if ns.repo not in RECIPES:
        sys.exit(f"no recipe for {ns.repo}; known: {', '.join(RECIPES)}")
    rows = [r for r in load_rows(ns.parquet) if r["repo"] == ns.repo]
    if ns.ids:
        want = set(ns.ids.split(","))
        rows = [r for r in rows if r["instance_id"] in want]
    rows = rows[:ns.limit] if not ns.ids else rows
    os.makedirs(ns.out_dir, exist_ok=True)
    out_path = os.path.join(ns.out_dir, f"classify_{ns.repo.replace('/', '__')}.jsonl")
    done = set()
    if os.path.exists(out_path):
        done = {json.loads(x)["instance_id"] for x in open(out_path).read().splitlines() if x.strip()}
    repo_dir = ensure_repo(ns.workdir, ns.repo)
    py = ensure_venv(ns.workdir, RECIPES[ns.repo])
    print(f"{len(rows)} instances, python {RECIPES[ns.repo]['python']}, results in {out_path}")
    for row in rows:
        if row["instance_id"] in done:
            print(f"  {row['instance_id']:28} (already in the file)")
            continue
        res = classify_instance(row, repo_dir, py, ns.timeout)
        with open(out_path, "a") as f:
            f.write(json.dumps(res) + "\n")
        types = sorted({t.get("exc_type") for t in res.get("tests", []) if t.get("exc_type")})
        print(f"  {res['instance_id']:28} {res['status']:18} {','.join(types) or res.get('detail', '')[:70]}"
              f"  [{res.get('seconds', '?')}s]", flush=True)
    rs = [json.loads(x) for x in open(out_path).read().splitlines() if x.strip()]
    from collections import Counter
    print("\nsummary:", dict(Counter(r["status"] for r in rs)))


if __name__ == "__main__":
    main()