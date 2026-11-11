"""Show how one R2E-Gym-Lite task stores its tests (read only, no Docker).

    uv run --with datasets python experiments/r2e/probe_task.py --commit <commit_hash>
"""
import argparse
import json


def cut(v, n=400):
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= n else s[:n] + f"... [{len(s)} chars]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", required=True)
    ap.add_argument("--dataset", default="R2E-Gym/R2E-Gym-Lite")
    ap.add_argument("--split", default="train")
    a = ap.parse_args()
    from datasets import load_dataset
    ds = load_dataset(a.dataset, split=a.split)
    rows = [r for r in ds if r["commit_hash"] == a.commit]
    if not rows:
        raise SystemExit("commit not found")
    r = rows[0]
    print("repo:", r["repo_name"], "| image:", r["docker_image"])
    print("modified_files:", r["modified_files"])
    print("relevant_files:", r["relevant_files"])
    print("\n--- problem_statement ---\n", cut(r["problem_statement"], 1500))
    ex = json.loads(r["execution_result_content"])
    print("\n--- execution_result_content: keys and values (shortened) ---")
    for k, v in ex.items():
        print(f"{k}: {cut(v, 300)}")
    exp = json.loads(r["expected_output_json"])
    print("\n--- expected_output_json:", len(exp), "tests; counts:",
          {s: list(exp.values()).count(s) for s in set(exp.values())})
    print(cut(list(exp.items()), 600))
    pc = json.loads(r["parsed_commit_content"])
    print("\n--- parsed_commit_content keys:", list(pc))
    for fd in pc.get("file_diffs", []):
        print({k: cut(v, 120) for k, v in fd.items()})


if __name__ == "__main__":
    main()