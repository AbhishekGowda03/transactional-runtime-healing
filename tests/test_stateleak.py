"""The StateLeak case list must be well formed (the runner relies on it)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "experiments", "stateleak"))
from cases import CASES  # noqa: E402


def test_ids_are_unique_and_labels_valid():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids))
    assert {c["label"] for c in CASES} == {"bad", "good", "reject-only"}


def test_good_cases_have_an_expected_state_and_bad_cases_do_not():
    for c in CASES:
        assert (c["expect"] is not None) == (c["label"] == "good"), c["id"]


def test_every_case_has_a_matching_crash_statement():
    for c in CASES:
        assert c["match"] in c["body"], c["id"]