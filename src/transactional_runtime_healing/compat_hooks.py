"""Effect events without audit hooks (a prototype for Python 3.7, which has no sys.addaudithook).

`sys.addaudithook` (PEP 578, Python 3.8) reports file, process and network operations from inside
the interpreter, whoever calls them. Python 3.7 has no such hook. This module produces the same
events by replacing the Python-visible entry points (os.remove, socket.socket.connect, open ...)
with wrappers that report the event to the same function (`protocol._audit_hook`) and then call
the original. Everything downstream (blocking, the file journal, the value-matching guard) is
unchanged. The event names and the argument tuples follow the audit event table of the Python
documentation.

What is lost compared with audit hooks (measured in experiments/compat, see the thesis notes):
  * Calls that kept a reference to the original function before install() ("from os import
    remove" in a module imported earlier) are not seen.
  * Calls from C code that does not go through the Python names (a C extension calling unlink()
    itself, `_socket.socket` / `_io.FileIO` used directly) are not seen. Audit hooks do not see
    native system calls either, but they do see the C-level callers of the interpreter's own
    functions.
  * Nothing can be removed again: install() is permanent, like an audit hook.

Set TXH_HOOKS=shim to use this module on a Python that has audit hooks (to compare the two).
"""
import builtins
import functools
import io
import os
import shutil
import signal
import socket
import subprocess
import sys

_installed = False
patched = []                                   # (owner name, attribute) pairs that were replaced


_wrappers = {}                                 # id(original function) -> (original, wrapper)


def _patch(owner, owner_name, name, event, extract, hook):
    orig = getattr(owner, name, None)
    if orig is None:
        return
    if id(orig) in _wrappers and not isinstance(owner, type):
        setattr(owner, name, _wrappers[id(orig)][1])   # same function under another name
        patched.append(f"{owner_name}.{name}")
        return

    def wrapper(*a, **k):
        try:
            ev = extract(*a, **k)
        except TypeError:
            ev = None                          # wrong call: let the original raise its own error
        if ev is not None:
            hook(event, ev)
        return orig(*a, **k)
    try:
        functools.update_wrapper(wrapper, orig)
    except Exception:
        pass
    setattr(owner, name, wrapper)
    if not isinstance(owner, type):            # methods of a class are patched for every caller
        _wrappers[id(orig)] = (orig, wrapper)
    patched.append(f"{owner_name}.{name}")


def _popen_event(self, args, bufsize=-1, executable=None, stdin=None, stdout=None, stderr=None,
                 preexec_fn=None, close_fds=True, shell=False, cwd=None, env=None, *a, **k):
    """The arguments of the audit event "subprocess.Popen": (executable, args, cwd, env), with the
    command line as the child will run it (a string becomes a list, shell=True adds /bin/sh -c)."""
    if isinstance(args, (str, bytes, os.PathLike)):
        args = [args]
    else:
        args = list(args)
    if shell:
        args = ["/bin/sh", "-c"] + args
        if executable:
            args[0] = executable
    if executable is None:
        executable = args[0]
    return (executable, args, cwd, env)


def _patch_pathlib_accessor(hook):
    """Python 3.7 to 3.10: Path.unlink, mkdir, rename ... call `self._accessor.<name>`, and the
    accessor class holds closures around os.unlink, os.mkdir ... made when pathlib was imported,
    so patching `os` afterwards never reaches them. Wrap the accessor entries themselves.
    (3.11 removed the accessor; there Path calls `os.*` directly and is already covered.)"""
    try:
        import pathlib
        acc = pathlib._NormalAccessor
    except Exception:
        return
    table = {                                  # accessor name -> (event, builds the event arguments)
        "unlink": ("os.remove", lambda p, *a: (os.fspath(p), None)),
        "rmdir": ("os.rmdir", lambda p, *a: (os.fspath(p), None)),
        "mkdir": ("os.mkdir", lambda p, mode=0o777, *a: (os.fspath(p), mode, None)),
        "rename": ("os.rename", lambda s, d, *a: (os.fspath(s), os.fspath(d), None, None)),
        "replace": ("os.rename", lambda s, d, *a: (os.fspath(s), os.fspath(d), None, None)),
        "chmod": ("os.chmod", lambda p, mode, *a: (os.fspath(p), mode, None)),
        "lchmod": ("os.chmod", lambda p, mode, *a: (os.fspath(p), mode, None)),
        "utime": ("os.utime", lambda p, times=None, *a, **k: (os.fspath(p), times, None, None)),
        "symlink": ("os.symlink", lambda s, d, *a: (os.fspath(s), os.fspath(d), None)),
        "link_to": ("os.link", lambda s, d, *a: (os.fspath(s), os.fspath(d), None, None)),
    }
    # The accessor's `open` is os.open(path, flags, mode) on 3.7 to 3.9 (Path.touch, Path.open's
    # opener) but io.open(path, mode, ...) on 3.10: the arguments differ, so look at what it is.
    op = acc.__dict__.get("open")
    op_fn = op.__func__ if isinstance(op, staticmethod) else op
    if getattr(op_fn, "__module__", "") in ("io", "_io"):
        table["open"] = ("open", lambda p, mode="r", *a, **k: (os.fspath(p), mode, None))
    else:
        table["open"] = ("open", lambda p, flags, mode=0o777, *a, **k: (os.fspath(p), None, flags))
    for name, (event, extract) in table.items():
        orig = acc.__dict__.get(name)
        if orig is None:
            continue
        fn = orig.__func__ if isinstance(orig, staticmethod) else orig

        def make(fn=fn, event=event, extract=extract):
            def wrapper(*a, **k):
                try:
                    ev = extract(*a, **k)
                except Exception:
                    ev = None
                if ev is not None:
                    hook(event, ev)
                return fn(*a, **k)
            return wrapper
        setattr(acc, name, staticmethod(make()))
        patched.append(f"pathlib._NormalAccessor.{name}")


def rebind_loaded_modules():
    """Point references that were taken before install() at the wrappers.

    Covers `from os import remove` in a module that is already imported, `posix.remove`,
    `_io.open`, and class attributes such as pathlib's accessor (Python 3.7 to 3.10 binds
    os.unlink, os.mkdir ... when pathlib is imported). Not covered: closures, default
    arguments, references held by C code.
    """
    n = 0
    for mod in list(sys.modules.values()):
        d = getattr(mod, "__dict__", None)
        if not isinstance(d, dict) or mod is sys.modules.get(__name__):
            continue
        for k, v in list(d.items()):
            hit = _wrappers.get(id(v))
            if hit is not None and hit[0] is v:
                try:
                    d[k] = hit[1]
                    n += 1
                except Exception:
                    pass
            elif isinstance(v, type):
                try:
                    items = list(vars(v).items())
                except Exception:
                    continue
                for ak, av in items:
                    wrap = None
                    if isinstance(av, (staticmethod, classmethod)):
                        hit = _wrappers.get(id(av.__func__))
                        if hit is not None and hit[0] is av.__func__:
                            wrap = type(av)(hit[1])
                    else:
                        hit = _wrappers.get(id(av))
                        if hit is not None and hit[0] is av:
                            wrap = hit[1]
                    if wrap is not None:
                        try:
                            setattr(v, ak, wrap)
                            n += 1
                        except Exception:
                            pass
    return n


def install(hook):
    """Replace the entry points once; `hook(event, args)` is protocol._audit_hook."""
    global _installed
    if _installed:
        return
    _installed = True
    P = lambda owner, oname, name, event, extract: _patch(owner, oname, name, event, extract, hook)

    # open: (path, mode, flags)
    P(builtins, "builtins", "open", "open", lambda file, mode="r", *a, **k: (file, mode, None))
    P(io, "io", "open", "open", lambda file, mode="r", *a, **k: (file, mode, None))
    P(os, "os", "open", "open", lambda path, flags, mode=0o777, *, dir_fd=None: (path, None, flags))

    # file system
    P(os, "os", "remove", "os.remove", lambda path, *, dir_fd=None: (path, dir_fd))
    P(os, "os", "unlink", "os.remove", lambda path, *, dir_fd=None: (path, dir_fd))
    P(os, "os", "rename", "os.rename", lambda src, dst, *, src_dir_fd=None, dst_dir_fd=None:
      (src, dst, src_dir_fd, dst_dir_fd))
    P(os, "os", "replace", "os.rename", lambda src, dst, *, src_dir_fd=None, dst_dir_fd=None:
      (src, dst, src_dir_fd, dst_dir_fd))
    P(os, "os", "mkdir", "os.mkdir", lambda path, mode=0o777, *, dir_fd=None: (path, mode, dir_fd))
    P(os, "os", "rmdir", "os.rmdir", lambda path, *, dir_fd=None: (path, dir_fd))
    P(os, "os", "truncate", "os.truncate", lambda path, length: (path, length))
    P(os, "os", "chmod", "os.chmod", lambda path, mode, *, dir_fd=None, follow_symlinks=True:
      (path, mode, dir_fd))
    P(os, "os", "chown", "os.chown", lambda path, uid, gid, *, dir_fd=None, follow_symlinks=True:
      (path, uid, gid, dir_fd))
    P(os, "os", "fchmod", "os.chmod", lambda fd, mode: (fd, mode, -1))
    P(os, "os", "lchmod", "os.chmod", lambda path, mode: (path, mode, -1))
    P(os, "os", "fchown", "os.chown", lambda fd, uid, gid: (fd, uid, gid, -1))
    P(os, "os", "lchown", "os.chown", lambda path, uid, gid: (path, uid, gid, -1))
    P(os, "os", "ftruncate", "os.truncate", lambda fd, length: (fd, length))
    P(os, "os", "utime", "os.utime", lambda path, times=None, *, ns=None, dir_fd=None,
      follow_symlinks=True: (path, times, ns, dir_fd))
    P(os, "os", "link", "os.link", lambda src, dst, *, src_dir_fd=None, dst_dir_fd=None,
      follow_symlinks=True: (src, dst, src_dir_fd, dst_dir_fd))
    P(os, "os", "symlink", "os.symlink", lambda src, dst, target_is_directory=False, *,
      dir_fd=None: (src, dst, dir_fd))
    P(shutil, "shutil", "rmtree", "shutil.rmtree", lambda path, *a, **k: (path, None, None))
    P(shutil, "shutil", "move", "shutil.move", lambda src, dst, *a, **k: (src, dst))

    # processes
    P(subprocess.Popen, "subprocess.Popen", "__init__", "subprocess.Popen", _popen_event)
    P(os, "os", "system", "os.system", lambda command: (command,))
    P(os, "os", "execv", "os.exec", lambda path, argv: (path, argv, None))
    P(os, "os", "execve", "os.exec", lambda path, argv, env: (path, argv, env))
    P(os, "os", "posix_spawn", "os.posix_spawn", lambda path, argv, env, *a, **k: (path, argv, env))
    P(os, "os", "posix_spawnp", "os.posix_spawn", lambda path, argv, env, *a, **k: (path, argv, env))
    P(os, "os", "fork", "os.fork", lambda: ())
    P(os, "os", "forkpty", "os.forkpty", lambda: ())
    P(os, "os", "kill", "os.kill", lambda pid, sig: (pid, sig))
    P(os, "os", "killpg", "os.killpg", lambda pgid, sig: (pgid, sig))
    P(signal, "signal", "pthread_kill", "signal.pthread_kill", lambda tid, sig: (tid, sig))

    # network
    S = socket.socket
    P(S, "socket.socket", "connect", "socket.connect", lambda self, address: (self, address))
    P(S, "socket.socket", "connect_ex", "socket.connect", lambda self, address: (self, address))
    P(S, "socket.socket", "sendto", "socket.sendto", lambda self, data, *a: (self, a[-1] if a else None))
    P(S, "socket.socket", "sendmsg", "socket.sendmsg", lambda self, buffers, *a: (self, a[-1] if a else None))
    P(socket, "socket", "getaddrinfo", "socket.getaddrinfo",
      lambda host, port, family=0, type=0, proto=0, flags=0: (host, port, family, type, proto))
    P(socket, "socket", "gethostbyname", "socket.gethostbyname", lambda name: (name,))
    P(socket, "socket", "gethostbyname_ex", "socket.gethostbyname", lambda name: (name,))

    # libraries and databases (patched only when the module is already importable)
    try:
        import ctypes
        P(ctypes.CDLL, "ctypes.CDLL", "__init__", "ctypes.dlopen", lambda self, name, *a, **k: (name,))
    except Exception:
        pass
    try:
        import sqlite3
        P(sqlite3, "sqlite3", "connect", "sqlite3.connect", lambda database, *a, **k: (database,))
    except Exception:
        pass
    _patch_pathlib_accessor(hook)
    if os.environ.get("TXH_SHIM_SWEEP", "1") != "0":
        rebind_loaded_modules()