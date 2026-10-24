"""Demo of the transactional protocol (v3), compared with plain healing.

The 'program' crashed because state['data'] is None. Each case offers a
different repair. For each one we show what the protocol decides, and what
happens to the ORIGINAL state.
"""
import os
import signal
import socket
import subprocess
import tempfile

from transactional_runtime_healing.protocol import plain_heal, transactional_heal

LEAK_FILE = os.path.join(tempfile.gettempdir(), "heal_demo_cache.txt")


def resume(state):
    """The rest of the program after the crash point."""
    return sum(state["data"]) + state["offset"]


def check_data_is_list(state):
    return isinstance(state.get("data"), list), "data must be a list"


def check_offset_kept(state):
    return "offset" in state, "offset must still exist"


CHECKS = [check_data_is_list, check_offset_kept]


# ---- repairs (in the real system these are written by an LLM) ----
def good_repair(state):
    state["data"] = [1, 2, 3]


def damaging_repair(state):
    state["data"] = [1, 2, 3]
    del state["offset"]


def crashing_repair(state):
    state["data"] = 1 / 0


def hanging_repair(state):
    while True:
        pass


def external_repair(state):
    state["data"] = [1]
    subprocess.run(["echo", "side effect"])


def swallowing_repair(state):
    state["data"] = [1]
    try:
        subprocess.run(["echo", "side effect"])
    except Exception:                  # repair hides the blocked effect
        pass


def os_system_repair(state):
    state["data"] = [1]
    os.system("echo side effect via os.system")


def udp_repair(state):
    state["data"] = [1]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(b"x", ("127.0.0.1", 9))
    s.close()


def noisy_good_repair(state):
    print("repair says: fixed data")   # output is replayed only on commit
    state["data"] = [1, 2, 3]


def noisy_bad_repair(state):
    print("repair says: this output must NOT appear")
    state["data"] = "not a list"


def file_repair(state):
    state["data"] = [1, 2, 3]
    with open(LEAK_FILE, "w") as f:
        f.write("written by repair")


# --- holes found in review of v2 ---
_pair = socket.socketpair()                  # connected socket inherited from parent
_peer_file = tempfile.NamedTemporaryFile("w+", delete=False)
_peer_file.write("0123456789")
_peer_file.flush()
_peer_file.seek(0)
_hits = []
signal.signal(signal.SIGUSR1, lambda *a: _hits.append(1))


def send_on_inherited_socket_repair(state):
    state["data"] = [1]
    _pair[0].send(b"leak")                     # no audit event for send()


def read_inherited_file_repair(state):
    state["data"] = [1]
    _peer_file.read(5)                         # moves the shared file offset


def kill_parent_repair(state):
    state["data"] = [1]
    os.kill(os.getppid(), signal.SIGUSR1)


def resume_original_error(state):
    raise TypeError("original error again")


def resume_new_error(state):
    raise KeyError("a different error")


def resume_unpicklable(state):
    return lambda: 1


# (name, repair, resume, original_error, also run plain healing?)
CASES = [
    ("good repair", good_repair, resume, None, True),
    ("damaging repair (deletes 'offset')", damaging_repair, resume, None, True),
    ("repair raises an exception", crashing_repair, resume, None, True),
    ("repair hangs", hanging_repair, resume, None, False),
    ("repair runs a subprocess", external_repair, resume, None, True),
    ("repair SWALLOWS the blocked effect", swallowing_repair, resume, None, False),
    ("repair uses os.system", os_system_repair, resume, None, True),
    ("repair sends a UDP packet", udp_repair, resume, None, True),
    ("good repair that prints", noisy_good_repair, resume, None, False),
    ("bad repair that prints", noisy_bad_repair, resume, None, False),
    ("repair writes a file (known limit)", file_repair, resume, None, True),
    ("repair sends on an inherited socket (KNOWN GAP)", send_on_inherited_socket_repair,
     resume, None, False),
    ("repair reads an inherited file", read_inherited_file_repair, resume, None, False),
    ("repair signals the parent process", kill_parent_repair, resume, None, False),
    ("repair ok, original error again later", good_repair, resume_original_error,
     TypeError, False),
    ("repair ok, NEW error later", good_repair, resume_new_error, TypeError, False),
    ("repair ok, result not picklable", good_repair, resume_unpicklable, None, False),
]

if __name__ == "__main__":
    for name, repair, res, orig_err, with_plain in CASES:
        if os.path.exists(LEAK_FILE):
            os.remove(LEAK_FILE)
        print(name, flush=True)
        original = {"data": None, "offset": 10}
        out = transactional_heal(original, repair, CHECKS, res, timeout=2,
                                 original_error=orig_err)
        untouched = original == {"data": None, "offset": 10}

        print(f"   protocol : {out.status.name} - {out.reason}")
        if out.committed:
            print(f"   continuation: {out.continuation}"
                  f"{', result=' + str(out.result) if out.continuation == 'ok' else ''}"
                  f"{', error=' + str(out.error) if out.error else ''}")
        if out.effects:
            print(f"   blocked effects  : {out.effects}")
        if out.fs_effects:
            print(f"   recorded fs writes: {out.fs_effects}")
        extra = ""
        if name.startswith("repair sends on an inherited"):
            _pair[1].setblocking(False)
            try:
                extra = f" | PEER RECEIVED {_pair[1].recv(10)!r}"
            except BlockingIOError:
                pass
        if name.startswith("repair signals"):
            extra = f" | parent got signal: {bool(_hits)}"
        if name.startswith("repair reads an inherited"):
            extra = f" | parent file offset: {_peer_file.tell()}"
        print(f"   original state untouched: {untouched}{extra}"
              f"{' | FILE LEFT BEHIND' if os.path.exists(LEAK_FILE) else ''}")

        if with_plain:
            if os.path.exists(LEAK_FILE):
                os.remove(LEAK_FILE)
            live = {"data": None, "offset": 10}
            try:
                r = plain_heal(live, repair, res)
                print(f"   plain    : continued, result={r}, state now {live}")
            except BaseException as e:
                print(f"   plain    : failed with {e!r}, state now {live}")
        print()
    if os.path.exists(LEAK_FILE):
        os.remove(LEAK_FILE)
    os.remove(_peer_file.name)