# Transactional Runtime Healing for LLM-Generated Repairs

MSc Data Science thesis project, University of Europe for Applied Sciences (UE Germany).
Author: Abhishek Gowda Thammanna Gowda.

## Idea

When a program crashes at runtime, an LLM can write repair code that lets execution continue
("runtime error healing"). That repair code runs in the live process, so a failed or unsafe
repair can leave changes behind.

This project tests a transactional protocol for such repairs. The repair is tried first in an
isolated child process (a `fork()` at the crash point). It is committed only if the checks pass.
Otherwise the child is discarded and the original process continues untouched.

Protocol steps: **Begin** (fork) -> **Trial** (run the repair in the child) ->
**Validate** (checks) -> **Commit** or **Restore**.

The novelty is not rollback itself (earlier work such as STEM/SEAD and Recovery Shepherding
used it), but its use for LLM-generated runtime healing.

## Status

Early stage (October 2026). So far: environment setup and an early feasibility test of `fork()`
isolation. The protocol itself is not implemented yet.

## Early fork test: results

Single runs on one machine (WSL2, Ubuntu, Python 3.12). To be repeated.

| Case | Result |
|---|---|
| dict / list | Parent unchanged (isolated) |
| numpy array | Parent unchanged (isolated) |
| pandas DataFrame | Parent unchanged (isolated) |
| numpy after heavy matrix product | Parent unchanged (isolated) |
| pandas after large groupby | Parent unchanged (isolated) |
| Lock held by another thread at fork | Child cannot get the lock |
| Thread pool open at fork | Child hangs (killed by timeout) |
| File on disk | Not isolated: child's write is visible to the parent |
| `fork()` time with ~1 GB in memory | about 2 ms |

Takeaways: in-memory state is isolated; hangs need a timeout (a hang counts as a rejected
repair); files need a separate filesystem layer (planned as a second step).

## Requirements

- Linux (the protocol uses `os.fork()`, which does not exist on Windows). On Windows use WSL2.
- Python 3.12
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
git clone https://github.com/AbhishekGowda03/transactional-runtime-healing.git
cd transactional-runtime-healing
uv sync
```

A pinned `requirements.txt` is also provided (for Docker or `pip install -r requirements.txt`).
Regenerate it after changing dependencies:

```bash
uv export --no-hashes --no-emit-project -o requirements.txt
```

## Running the fork tests

```bash
uv run python experiments/forktest/fork_test.py
uv run python experiments/forktest/fork_test_heavy.py   # takes about 30 s (one case waits for a hang)
```