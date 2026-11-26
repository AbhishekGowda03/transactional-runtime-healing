"""Regenerate sample_mod.ast.z after the instrumenter changed (needs Python 3.9 or newer):

    uv run python tests/fixtures/pre_instrument/make.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "src"))
from transactional_runtime_healing import pre_instrument  # noqa: E402

NAME = "/fixture/sample_mod.py"                 # the file name stored in the entry

if __name__ == "__main__":
    with open(os.path.join(HERE, "sample_mod.py"), "rb") as f:
        raw = f.read()
    with open(os.path.join(HERE, "sample_mod.ast.z"), "wb") as f:
        f.write(pre_instrument.build_entry(raw, NAME))
    print("written", os.path.join(HERE, "sample_mod.ast.z"))