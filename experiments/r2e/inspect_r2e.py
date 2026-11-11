"""First look at R2E-Gym-Lite (the fallback benchmark the supervisor suggested).

    uv run --with datasets python experiments/r2e/inspect_r2e.py [--split train] [--out results/r2e_inspect.json]

Needs internet access to huggingface.co (the dataset is downloaded once, no Docker needed).
It only READS the data and answers the questions that decide whether runtime healing can be
tried on it:
  * which columns exist (task text, repo, gold patch, tests, Docker image name ...)
  * how many instances per repository
  * how the tests are stored (a command, a test file, a list of test ids?)
  * how many gold patches change only Python files
  * a few example rows, shortened, so the format is visible
Nothing is executed and no key is used. Paste the printed summary back.
"""
import argparse
import collections
import json
import os


def short(v, n=300):
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= n else s[:n] + f"... [{len(s)} chars]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="R2E-Gym/R2E-Gym-Lite")
    ap.add_argument("--split", default="train")
    ap.add_argument("--out", default="results/r2e_inspect.json")
    a = ap.parse_args()
    from datasets import load_dataset
    ds = load_dataset(a.dataset, split=a.split)
    print("rows:", len(ds))
    print("columns:", {k: str(v) for k, v in ds.features.items()})
    cols = ds.column_names
    repo_col = next((c for c in ("repo_name", "repo", "repository") if c in cols), None)
    per_repo = collections.Counter(ds[repo_col]) if repo_col else {}
    print("per repository:", dict(per_repo.most_common()))
    patch_col = next((c for c in ("patch", "gold_patch", "ground_truth_patch") if c in cols), None)
    py_only = 0
    if patch_col:
        for p in ds[patch_col]:
            files = [l.split(" b/")[-1] for l in (p or "").splitlines() if l.startswith("diff --git")]
            py_only += bool(files) and all(f.endswith(".py") for f in files)
        print("gold patches touching only .py files:", py_only, "of", len(ds))
    examples = []
    for i in (0, len(ds) // 2, len(ds) - 1):
        row = ds[i]
        examples.append({k: short(v) for k, v in row.items()})
    print("example rows (shortened):")
    for e in examples:
        print(json.dumps(e, indent=1)[:2500])
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(dict(rows=len(ds), columns=cols, per_repo=dict(per_repo),
                       py_only_patches=py_only, examples=examples), f, indent=1)
    print("saved:", a.out)


if __name__ == "__main__":
    main()