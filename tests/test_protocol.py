"""Tests for the transactional healing protocol (one test per behaviour).

Run:  uv run pytest -v
"""
import os
import signal
import socket
import subprocess
import tempfile

import pytest

from transactional_runtime_healing.protocol import (
    Status, error_site, plain_heal, transactional_heal)

START = {"data": None, "offset": 10}


def resume(state):
    return sum(state["data"]) + state["offset"]


def check_data_is_list(state):
    return isinstance(state.get("data"), list), "data must be a list"


def check_offset_kept(state):
    return "offset" in state, "offset must still exist"


CHECKS = [check_data_is_list, check_offset_kept]


def heal(repair, res=resume, **kw):
    state = dict(START)
    out = transactional_heal(state, repair, CHECKS, res, timeout=kw.pop("timeout", 3), **kw)
    return out, state


def good(state):
    state["data"] = [1, 2, 3]


# ---- accepted / rejected by the checks ----
def test_good_repair_commits_and_original_untouched():
    out, state = heal(good)
    assert out.status is Status.COMMITTED
    assert out.continuation == "ok" and out.result == 16
    assert state == START


def test_damaging_repair_rejected_by_check():
    def bad(state):
        state["data"] = [1]
        del state["offset"]
    out, state = heal(bad)
    assert out.status is Status.REJECTED_CHECK
    assert state == START
    with pytest.raises(KeyError):             # baseline: plain healing is damaged
        plain_heal(dict(START), bad, resume)


def test_repair_exception_rejected():
    out, state = heal(lambda s: 1 / 0)
    assert out.status is Status.REJECTED_REPAIR_ERROR
    assert state == START


def test_hanging_repair_times_out():
    def hang(state):
        while True:
            pass
    out, state = heal(hang, timeout=1)
    assert out.status is Status.REJECTED_TIMEOUT
    assert state == START


# ---- external effects ----
def _subprocess(state):
    state["data"] = [1]
    subprocess.run(["echo", "x"])


def _swallowed(state):
    state["data"] = [1]
    try:
        subprocess.run(["echo", "x"])
    except Exception:
        pass


def _os_system(state):
    state["data"] = [1]
    os.system("true")


def _udp(state):
    state["data"] = [1]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(b"x", ("127.0.0.1", 9))


def _kill_parent(state):
    state["data"] = [1]
    os.kill(os.getppid(), signal.SIGUSR1)


@pytest.mark.parametrize("repair,event", [
    (_subprocess, "subprocess.Popen"),
    (_swallowed, "subprocess.Popen"),          # exception caught by repair: still rejected
    (_os_system, "os.system"),
    (_udp, "socket.sendto"),
    (_kill_parent, "os.kill"),
])
def test_external_effects_rejected(repair, event):
    out, state = heal(repair)
    assert out.status is Status.REJECTED_EXTERNAL_EFFECT
    assert event in out.effects
    assert state == START


def test_kill_does_not_reach_parent():
    hits = []
    old = signal.signal(signal.SIGUSR1, lambda *a: hits.append(1))
    try:
        heal(_kill_parent)
    finally:
        signal.signal(signal.SIGUSR1, old)
    assert hits == []


# ---- inherited file descriptors ----
def test_inherited_file_offset_rejected_and_restored():
    with tempfile.TemporaryFile("w+") as f:
        f.write("0123456789")
        f.flush()
        f.seek(0)

        def read_it(state):
            state["data"] = [1]
            f.read(5)
        out, state = heal(read_it)
        assert out.status is Status.REJECTED_INHERITED_FD
        assert out.fd_effects and out.effects == []   # fd changes are listed separately
        assert f.tell() == 0                    # parent offset restored


def test_known_gap_send_on_inherited_socket_is_not_detected():
    a, b = socket.socketpair()
    b.setblocking(False)

    def send(state):
        state["data"] = [1]
        a.send(b"leak")
    out, _ = heal(send)
    assert out.status is Status.COMMITTED       # documents the gap, not a goal
    assert b.recv(10) == b"leak"


# ---- output handling ----
def test_output_replayed_only_on_commit(capfd):
    def noisy_good(state):
        print("GOOD-OUT")
        state["data"] = [1]

    def noisy_bad(state):
        print("BAD-OUT")
        state["data"] = "x"
    heal(noisy_good)
    heal(noisy_bad)
    out = capfd.readouterr().out
    assert "GOOD-OUT" in out and "BAD-OUT" not in out


def test_output_held_back_at_fd_level(capfd):
    def fd_good(state):
        os.write(1, b"FD-GOOD\n")
        state["data"] = [1]

    def fd_bad(state):
        os.write(1, b"FD-BAD\n")
        os.write(2, b"FD-BAD-ERR\n")
        state["data"] = "x"
    heal(fd_good)
    heal(fd_bad)
    cap = capfd.readouterr()
    assert "FD-GOOD" in cap.out
    assert "FD-BAD" not in cap.out and "FD-BAD" not in cap.err


# ---- filesystem: recorded, not blocked (known limit) ----
def test_file_write_recorded_but_not_undone(tmp_path):
    target = tmp_path / "leak.txt"

    def write_file(state):
        state["data"] = [1]
        target.write_text("x")
    out, _ = heal(write_file)
    assert out.status is Status.COMMITTED
    assert any(str(target) in e for e in out.fs_effects)
    assert target.exists()                      # limit: the CoW layer is future work


# ---- continuation outcomes are separate from the repair verdict ----
def test_original_error_again():
    def again(state):
        raise TypeError("x")
    out, _ = heal(good, again, original_error=TypeError)
    assert out.committed and out.continuation == "original_error_again"


def test_new_error():
    def other(state):
        raise KeyError("y")
    out, _ = heal(good, other, original_error=TypeError)
    assert out.committed and out.continuation == "new_error"


def test_same_type_other_site_is_new_error():
    def raiser(state):
        raise TypeError("z")
    out, _ = heal(good, raiser, original_error=TypeError,
                  original_site=("test_protocol.py", 1))
    assert out.continuation == "new_error"


def test_same_site_is_original_error_again():
    def raiser(state):
        raise TypeError("z")                   # SITE
    try:
        raiser({})
    except TypeError as e:
        site = error_site(e)                    # (full path, line) of the raise
    out, _ = heal(good, raiser, original_error=TypeError, original_site=site)
    assert out.continuation == "original_error_again"
    out, _ = heal(good, raiser, original_error=TypeError,
                  original_site=(os.path.basename(site[0]), site[1]))   # basename also accepted
    assert out.continuation == "original_error_again"


def test_result_not_transferable():
    out, _ = heal(good, lambda s: (lambda: 1))
    assert out.committed and out.continuation == "result_not_transferable"


def test_continuation_has_its_own_timeout():
    import time

    def slow(state):
        time.sleep(2)
        return 1
    out, _ = heal(good, slow, timeout=1, continuation_timeout=0.3)
    assert out.committed and out.continuation == "timeout"
    out, _ = heal(good, lambda s: time.sleep(1.2) or 5, timeout=0.5)
    assert out.committed and out.continuation == "ok" and out.result == 5