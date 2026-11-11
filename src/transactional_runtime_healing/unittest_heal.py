"""Run a unittest-based test script under runtime healing (for runners that are not pytest).

    python -m transactional_runtime_healing.unittest_heal SCRIPT [args...]

Same settings as the pytest plug-in (pytest_plugin.py: TXH_HEAL_ROOT, TXH_HEAL_MODE,
TXH_HEAL_LOG, TXH_HEAL_ONLY, TXH_LLM_EVENTS, TXH_HEAL_STATS ...); healing is off unless
TXH_HEAL_ROOT is set. Repairs are only requested while a test method runs (setUp, tearDown
and imports are not healed). SCRIPT is run as __main__ with the current directory unchanged.
R2E-Gym tasks use such a script (tornado_unittest_runner.py).
"""
import json
import os
import runpy
import sys
import unittest

from transactional_runtime_healing import pytest_plugin as PP   # installs the import hook


def _patch():
    orig = unittest.TestCase._callTestMethod

    def call(self, method):
        PP.ACTIVE[0] = True
        try:
            return orig(self, method)
        finally:
            PP.ACTIVE[0] = False
    unittest.TestCase._callTestMethod = call


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        raise SystemExit(__doc__)
    _patch()
    sys.argv = argv
    try:
        runpy.run_path(argv[0], run_name="__main__")
    finally:
        if PP.FINDER is not None:
            print(f"runtime healing: {len(PP.FINDER.instrumented)} modules instrumented, "
                  f"{len(PP.FINDER.skipped)} left unchanged", file=sys.stderr)
        path = os.environ.get("TXH_HEAL_STATS")
        if path and PP.PROPOSE is not None:
            with open(path, "w") as f:
                json.dump(PP.PROPOSE.stats, f)


if __name__ == "__main__":
    main()