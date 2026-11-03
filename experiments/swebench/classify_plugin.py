"""pytest plugin used by classify.py: writes one JSON line per test result, with the type of the
exception and where it was raised. Loaded with `-p classify_plugin`.

Environment: CLASSIFY_OUT (file to append to), CLASSIFY_ROOT (the repository folder).
"""
import json
import os

import pytest

OUT = os.environ.get("CLASSIFY_OUT", "classify_results.jsonl")
ROOT = os.path.realpath(os.environ.get("CLASSIFY_ROOT", os.getcwd()))


def _is_test_path(path: str) -> bool:
    base = os.path.basename(path)
    return (base.startswith("test_") or base == "conftest.py" or "/tests/" in path
            or "/testing/" in path or "/site-packages/" in path or "/_pytest/" in path)


def _repo_code(path: str) -> bool:
    p = os.path.realpath(path)
    return p.startswith(ROOT + os.sep) and not _is_test_path(p)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call" or (rep.when in ("setup", "teardown") and rep.failed):
        rec = dict(nodeid=item.nodeid, name=item.name, when=rep.when, outcome=rep.outcome)
        if call.excinfo is not None:
            et = call.excinfo.type
            rec["exc_type"] = et.__name__
            rec["exc_msg"] = str(call.excinfo.value)[:200]
            tb = list(call.excinfo.traceback)
            if tb:
                last = tb[-1]
                rec["raised_at"] = f"{os.path.relpath(str(last.path), ROOT)}:{last.lineno + 1}"
                rec["raised_in_repo_code"] = _repo_code(str(last.path))
                rec["repo_frames"] = sum(1 for e in tb if _repo_code(str(e.path)))
        with open(OUT, "a") as f:
            f.write(json.dumps(rec) + "\n")