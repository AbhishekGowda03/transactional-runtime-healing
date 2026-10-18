"""Heavier fork test: fork AFTER numpy/pandas have done real work.

Big numpy and pandas operations start internal worker threads (BLAS/OpenMP,
thread pools). Those threads do not exist in a forked child, which can make
the child hang or crash. Each case here first does heavy work in the parent,
then forks, then lets the child use the library again.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from fork_test import run_in_child  # reuse the helper from the first test

TIMEOUT = 30


def case_numpy_after_blas():
    a = np.random.rand(1500, 1500)
    _ = a @ a  # heavy matrix product: starts the BLAS thread pool
    arr = np.arange(5)

    def child():
        b = a @ a  # child uses BLAS again
        arr[0] = 99
        return f"child matmul ok, checksum={b.sum():.1f}"

    msg = run_in_child(child, TIMEOUT)
    return msg, arr[0] == 0


def case_pandas_after_groupby():
    n = 1_000_000
    df = pd.DataFrame({"k": np.random.randint(0, 1000, n), "v": np.random.rand(n)})
    _ = df.groupby("k")["v"].sum()  # heavy pandas work first

    def child():
        s = df.groupby("k")["v"].sum()  # child uses pandas again
        df.loc[0, "v"] = -1.0
        return f"child groupby ok, groups={len(s)}"

    original = df.loc[0, "v"]
    msg = run_in_child(child, TIMEOUT)
    return msg, df.loc[0, "v"] == original


def case_thread_pool():
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda x: x * x, range(100)))  # pool used, then closed
    pool = ThreadPoolExecutor(max_workers=4)
    list(pool.map(lambda x: x * x, range(100)))  # pool left OPEN at fork time

    def child():
        res = list(pool.map(lambda x: x + 1, range(10)))  # reuse the old pool
        return f"child reused old pool, result length={len(res)}"

    msg = run_in_child(child, TIMEOUT)
    pool.shutdown()
    return msg, None


def case_fork_latency():
    big = np.ones(125_000_000)  # about 1 GB of memory
    t0 = time.perf_counter()
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    t1 = time.perf_counter()
    os.waitpid(pid, 0)
    del big
    return f"fork() call took {(t1 - t0) * 1000:.1f} ms with ~1 GB in memory", None


CASES = [
    ("numpy after heavy matmul", case_numpy_after_blas),
    ("pandas after big groupby", case_pandas_after_groupby),
    ("open thread pool", case_thread_pool),
    ("fork time with 1 GB memory", case_fork_latency),
]

if __name__ == "__main__":
    for name, fn in CASES:
        msg, unchanged = fn()
        if unchanged is None:
            parent = "n/a"
        else:
            parent = "unchanged (isolated)" if unchanged else "CHANGED (leaked)"
        print(f"{name:27} | parent: {parent:21} | {msg}")