import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "experiments", "swebench"))
import inspect_lite as I  # noqa: E402

ROWS = [
    dict(instance_id="a__a-1", repo="a/a", version="1.0", base_commit="c1",
         FAIL_TO_PASS=json.dumps(["t1"]), patch="diff --git a/x b/x\n"),
    dict(instance_id="a__a-2", repo="a/a", version="1.1", base_commit="c2",
         FAIL_TO_PASS=json.dumps(["t1", "t2", "t3", "t4"]), patch="diff --git a/x b/x\n"),
    dict(instance_id="b__b-1", repo="b/b", version="2", base_commit="c3",
         FAIL_TO_PASS=["t1"], patch="diff --git a/x b/x\ndiff --git a/y b/y\n"),
    dict(instance_id="b__b-2", repo="b/b", version="2", base_commit="c4",
         FAIL_TO_PASS="not json", patch=""),
]


def test_summarize_counts_and_filters():
    per, cands = I.summarize(ROWS)
    assert per["a/a"]["n"] == 2 and per["a/a"]["easy"] == 1 and per["b/b"]["n"] == 2
    assert [c["instance_id"] for c in cands] == ["a__a-1"]
    per, cands = I.summarize(ROWS, repo="b/b", max_patch_files=2)
    assert [c["instance_id"] for c in cands] == ["b__b-1"] and "a/a" not in per