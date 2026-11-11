"""How does each R2E-Gym-Lite repository start its tests? (one task per repo; pulls 10 images)

    uv run --with datasets python experiments/r2e/peek_repos.py [--repos tornado,scrapy,pyramid]

For one task per repository it prints: the test file names in the dataset, the image's
run_tests.sh, the Python version, and the top of /testbed. Nothing is changed.
Each image is 300-500 MB; use --repos to look at a few only.
"""
import argparse
import subprocess


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", default="")
    a = ap.parse_args()
    from datasets import load_dataset
    import json
    ds = load_dataset("R2E-Gym/R2E-Gym-Lite", split="train")
    first, want = {}, [r for r in a.repos.split(",") if r]
    for r in ds:
        if r["repo_name"] not in first and (not want or r["repo_name"] in want):
            first[r["repo_name"]] = r
    for name, r in sorted(first.items()):
        ex = r["execution_result_content"]
        ex = json.loads(ex) if isinstance(ex, str) else ex
        print("=" * 70)
        print(name, r["commit_hash"][:12], "| test files:", ex["test_file_names"])
        cmd = ("cat /testbed/run_tests.sh; echo ---; /testbed/.venv/bin/python --version; "
               "ls /testbed | head -25 | tr '\\n' ' '; echo; "
               "/testbed/.venv/bin/python -c 'import sys; print(sys.version_info[:2])'")
        p = subprocess.run(["docker", "run", "--rm", r["docker_image"], "bash", "-lc", cmd],
                           capture_output=True, text=True)
        print((p.stdout + p.stderr).strip()[:1500])


if __name__ == "__main__":
    main()