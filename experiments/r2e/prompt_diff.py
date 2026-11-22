"""Find crash points whose prompt differs between settings, and show what differs.

The model cache (results/llm_cache_r2e) holds one file per request, with the full prompt. Two
requests belong to the same crash point when they have the same run number and the same first
two lines of the prompt (file, line, function and exception). If one crash point has more than
one prompt, the settings (or processes) did not see the same text.

Caution: the cache does not record the setting, so a crash point that is hit several times (a
loop, parametrized tests, several tasks of one repository) also shows several variants. Read the
printed lines: text that is different in every process (a port, a descriptor number, an address)
is a leak that should be normalised; different values of the program's own data are not.

  python experiments/r2e/prompt_diff.py                       # summary and the first 6 diffs
  python experiments/r2e/prompt_diff.py --grep s3.py --show 20
"""
import argparse
import collections
import difflib
import glob
import json
import os


def load(cache_dir):
    recs = []
    for path in glob.glob(os.path.join(cache_dir, "*.json")):
        try:
            d = json.load(open(path, encoding="utf-8"))
            user = next(m["content"] for m in d["messages"] if m["role"] == "user")
        except (OSError, ValueError, StopIteration, KeyError):
            continue
        recs.append((str(d.get("run_id") or ""), user, d.get("created", "")))
    return recs


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.path.join(os.path.dirname(__file__), "..", "..",
                                                    "results", "llm_cache_r2e"))
    ap.add_argument("--grep", default="", help="only crash points whose first lines contain this")
    ap.add_argument("--show", type=int, default=6, help="how many differing crash points to print")
    ap.add_argument("--include-leaked", action="store_true",
                    help="also use prompts that contain the instrumentation's own variables "
                         "(`__eh_e`, `_Class__eh_r`; the cache still holds those from before the fix)")
    a = ap.parse_args(argv)
    recs = load(a.cache)
    leaked = [r for r in recs if "__eh_" in r[1]]
    if not a.include_leaked:
        recs = [r for r in recs if "__eh_" not in r[1]]
        print(f"{len(leaked)} cached prompts from before the fix left out (they contain `__eh_`)")
    groups = collections.defaultdict(set)
    for run, user, _ in recs:
        head = "\n".join(user.splitlines()[:2])
        if a.grep in head:
            groups[(run, head)].add(user)
    multi = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"{len(recs)} cached requests, {len(groups)} crash points (per run), "
          f"{len(multi)} with more than one prompt")
    for (run, head), prompts in sorted(multi.items())[:a.show]:
        ps = sorted(prompts)
        print(f"\n=== run {run or '-'}: {head.splitlines()[0]} | {head.splitlines()[1][:100]}"
              f"  ({len(ps)} variants)")
        for line in difflib.unified_diff(ps[0].splitlines(), ps[1].splitlines(), lineterm="", n=0):
            if line.startswith(("---", "+++", "@@")):
                continue
            print("  " + line[:220])


if __name__ == "__main__":
    main()