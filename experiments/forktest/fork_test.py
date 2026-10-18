"""Early fork test: does a forked child stay isolated from the parent?

Each case: set up some state in the parent, fork, let the child change it,
then check whether the parent still sees the original state.
"""
import os
import tempfile
import threading
import time

import numpy as np
import pandas as pd


def run_in_child(modify, timeout=10):
    """Fork. The child runs modify() and reports back through a pipe.
    The parent waits (max `timeout` seconds) and returns a short message."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # ---- child process ----
        os.close(r)
        code = 0
        try:
            msg = modify() or "ok"
        except BaseException as e:
            msg = f"error: {e!r}"
            code = 1
        os.write(w, msg.encode())
        os._exit(code)  # leave immediately, without parent's cleanup
    # ---- parent process ----
    os.close(w)
    start = time.time()
    while True:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        if time.time() - start > timeout:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            os.close(r)
            return "HANG: child killed after timeout"
        time.sleep(0.05)
    msg = os.read(r, 1000).decode()
    os.close(r)
    return f"exit={os.waitstatus_to_exitcode(status)}, {msg}"


def case_dict():
    state = {"items": [1, 2, 3]}

    def child():
        state["items"].append(99)

    msg = run_in_child(child)
    return msg, state == {"items": [1, 2, 3]}


def case_numpy():
    arr = np.arange(5)

    def child():
        arr[0] = 99  # in-place change

    msg = run_in_child(child)
    return msg, arr[0] == 0


def case_pandas():
    df = pd.DataFrame({"x": [1, 2, 3]})

    def child():
        df.loc[0, "x"] = 99  # in-place change

    msg = run_in_child(child)
    return msg, df.loc[0, "x"] == 1


def case_thread():
    lock = threading.Lock()
    stop = threading.Event()

    def holder():  # background thread that keeps a lock held
        with lock:
            stop.wait()

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    time.sleep(0.2)

    def child():
        got = lock.acquire(timeout=1)  # the holder thread does not exist here
        return f"threads in child={threading.active_count()}, lock acquired={got}"

    msg = run_in_child(child)
    stop.set()
    t.join()
    return msg, None  # parent state not the point of this case


def case_file():
    path = tempfile.mktemp()
    with open(path, "w") as f:
        f.write("original")

    def child():
        with open(path, "w") as f:
            f.write("changed by child")

    msg = run_in_child(child)
    with open(path) as f:
        unchanged = f.read() == "original"
    os.remove(path)
    return msg, unchanged


CASES = [
    ("dict / list", case_dict),
    ("numpy array", case_numpy),
    ("pandas DataFrame", case_pandas),
    ("background thread + lock", case_thread),
    ("file on disk", case_file),
]

if __name__ == "__main__":
    for name, fn in CASES:
        msg, unchanged = fn()
        if unchanged is None:
            parent = "n/a"
        else:
            parent = "unchanged (isolated)" if unchanged else "CHANGED (leaked)"
        print(f"{name:26} | parent: {parent:21} | child: {msg}")