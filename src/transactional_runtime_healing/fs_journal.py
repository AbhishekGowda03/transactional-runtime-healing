"""Filesystem journal: undo the file changes of a rejected repair.

The live protocol runs a repair in a forked child. Memory, the working directory and the
environment of the child vanish with it, but files do not. This module closes that gap with
an undo log (a journal of before-images):

* The child, running under the audit hook, calls `FsJournal.before(event, args)` BEFORE each
  file operation that changes the filesystem. The journal saves what is needed to reverse it
  (a copy or hard link of the old file, the old size, the old mode, "did not exist" ...) in a
  private directory and appends one line to a manifest. The directory lives outside the
  program's own folders and survives the child (a rejected child is killed).
* If the repair is rejected (or the child dies, or times out) the PARENT calls
  `FsJournal.undo()`: the entries are replayed in reverse order. If it is committed the child
  calls `discard()`: the files stay as the repair left them.
* Files the parent had already open for writing (a log file, say) change without any audit
  event when the child writes to them (the open file is shared after fork). `mark_fds()` is
  called by the parent before the fork and remembers their size (and, for small files, their
  content); `undo_fds()` cuts them back.

What is covered: open for writing (create, truncate, append, overwrite), os.remove/unlink,
os.rename/replace, os.mkdir/makedirs, os.rmdir, os.truncate, os.chmod, os.chown, os.utime,
os.link, os.symlink, shutil.rmtree/move/copy* (through the operations they call), sqlite3.connect
(database and its -journal/-wal/-shm files), and writes through inherited file descriptors.

What is not covered (stated so the claim is not larger than the code):
* A change the journal cannot reverse is refused instead: if a file is larger than
  `max_file` bytes, the journal exceeds `max_total` bytes, or the file is a device or pipe,
  `before` raises `Unjournalable` and the trial is rejected as an external effect.
* Writes through inherited descriptors are undone exactly only for files up to `FD_COPY`
  bytes; larger ones are cut back to their old length (appends are undone, an in-place
  overwrite is not) and the result says so.
* Writes that bypass the audit hook (a C extension, mmap writes, another process) are not
  seen. Directory-relative `os.open(..., dir_fd=...)` of a RELATIVE path is not resolved.
* Undo trusts that nobody else changed the same files meanwhile (a second process).
* Extended attributes, ACLs and file-system-level timestamps other than atime/mtime are not
  restored. Only Linux (/proc) is supported for descriptor handling.
"""
import errno
import fcntl
import json
import os
import shutil
import stat
import tempfile
from typing import Dict, List, Tuple

MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
FD_COPY = 4 * 1024 * 1024

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

# audit events other than "open" that change the filesystem
EVENTS = frozenset({
    "os.remove", "os.rmdir", "os.mkdir", "os.rename", "os.truncate", "os.chmod", "os.chown",
    "os.utime", "os.link", "os.symlink", "sqlite3.connect",
})


class Unjournalable(Exception):
    """The operation cannot be reversed, so it must not be allowed to run."""


def _fsdecode(p):
    return os.fsdecode(p) if isinstance(p, (str, bytes, os.PathLike)) else p


def _abs(path, dir_fd=None):
    """Absolute path of an argument of a file operation (no normalisation: `..` and symlinks
    are left to the operating system)."""
    if isinstance(path, int):                    # an open descriptor
        return os.readlink(f"/proc/self/fd/{path}")
    p = _fsdecode(path)
    if os.path.isabs(p):
        return p
    if isinstance(dir_fd, int) and dir_fd >= 0:
        return os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), p)
    return os.path.join(os.getcwd(), p)


def _lstat(p):
    try:
        return os.lstat(p)
    except FileNotFoundError:
        return None


def _opens_for_writing(mode, flags):
    return (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
           (isinstance(flags, int) and bool(flags & _WRITE_FLAGS))


class FsJournal:
    def __init__(self, directory, max_file=MAX_FILE, max_total=MAX_TOTAL):
        self.dir = directory
        self.max_file, self.max_total = max_file, max_total
        self.manifest = os.path.join(directory, "manifest.jsonl")
        self._n = 0
        self._bytes = 0
        self._level: Dict[str, int] = {}          # path -> 1 size recorded, 2 full image recorded
        self._fds: List[dict] = []                # inherited descriptors (parent side)

    @classmethod
    def create(cls, base=None, **kw):
        return cls(tempfile.mkdtemp(prefix="txh-journal-", dir=base), **kw)

    # ------------------------------------------------------------------
    # recording (child)
    # ------------------------------------------------------------------
    def _write(self, entry):
        fd = os.open(self.manifest, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(entry) + "\n").encode())
        finally:
            os.close(fd)

    def _backup_name(self):
        self._n += 1
        return f"b{self._n}"

    def _save_file(self, path, st, link_ok):
        """Keep the old content of a regular file: a hard link (cheap, exact; only valid when
        the operation does not write into the inode) or a copy."""
        name = self._backup_name()
        dest = os.path.join(self.dir, name)
        how = "copy"
        if link_ok:
            try:
                os.link(path, dest)
                how = "link"
            except OSError:
                how = "copy"
        if how == "copy":
            if st.st_size > self.max_file:
                raise Unjournalable(f"{path} is {st.st_size} bytes (limit {self.max_file})")
            if self._bytes + st.st_size > self.max_total:
                raise Unjournalable(f"the journal would exceed {self.max_total} bytes")
            shutil.copyfile(path, dest)
            self._bytes += st.st_size
        self._write(dict(k="file", path=path, b=name, how=how, mode=stat.S_IMODE(st.st_mode),
                         at=st.st_atime_ns, mt=st.st_mtime_ns))

    def _image(self, path, link_ok=False, full=True):
        """Record the state of `path` as it is now."""
        st = _lstat(path)
        if st is None:
            self._write(dict(k="absent", path=path))
        elif stat.S_ISREG(st.st_mode):
            if full:
                self._save_file(path, st, link_ok)
            else:
                self._write(dict(k="size", path=path, size=st.st_size,
                                 at=st.st_atime_ns, mt=st.st_mtime_ns))
        elif stat.S_ISLNK(st.st_mode):
            self._write(dict(k="symlink", path=path, target=os.readlink(path)))
        elif stat.S_ISDIR(st.st_mode):
            self._write(dict(k="dir", path=path, mode=stat.S_IMODE(st.st_mode),
                             at=st.st_atime_ns, mt=st.st_mtime_ns))
        else:
            raise Unjournalable(f"{path} is a special file")

    def _content(self, path, level):
        """Image for an operation that writes INTO a file (level 1: only grows, 2: anything)."""
        if self._level.get(path, 0) >= level:
            return
        st = _lstat(path)
        if st is not None and not stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode):
            return                               # a directory (open fails) or special file
        self._image(path, full=(level == 2))
        self._level[path] = level

    def before(self, event, args):
        """Called by the audit hook before a filesystem operation (any event)."""
        if event == "open":
            path, mode, flags = args
            if isinstance(path, int) or not _opens_for_writing(mode, flags):
                return
            real = os.path.realpath(_abs(path))
            if "__pycache__" in real:
                return
            st = _lstat(real)
            if st is not None and not stat.S_ISREG(st.st_mode):
                return                           # directory, pipe, tty, /dev/null ...
            f = flags if isinstance(flags, int) else 0
            if not f:                             # no flags given: derive them from the mode
                m = mode if isinstance(mode, str) else ""
                f = (os.O_APPEND if "a" in m else 0) | (os.O_TRUNC if "w" in m else 0) | \
                    (os.O_RDWR if "+" in m else os.O_WRONLY)
            append_only = bool(f & os.O_APPEND) and not f & (os.O_RDWR | os.O_TRUNC)
            truncates = bool(f & os.O_TRUNC)
            self._content(real, 1 if append_only and not truncates else 2)
            return
        if event not in EVENTS:
            return
        if event == "os.remove":
            path = _abs(args[0], args[1] if len(args) > 1 else None)
            st = _lstat(path)
            if st is not None and stat.S_ISDIR(st.st_mode):
                return                           # the call will fail
            self._image(path, link_ok=True)
            self._level.pop(path, None)
        elif event == "os.rmdir":
            path = _abs(args[0], args[1] if len(args) > 1 else None)
            st = _lstat(path)
            if st is not None and stat.S_ISDIR(st.st_mode):
                self._image(path)
        elif event == "os.mkdir":
            path = _abs(args[0], args[2] if len(args) > 2 else None)
            if _lstat(path) is None:
                self._write(dict(k="absent", path=path))
        elif event == "os.rename":
            src = _abs(args[0], args[2] if len(args) > 2 else None)
            dst = _abs(args[1], args[3] if len(args) > 3 else None)
            if _lstat(src) is None:
                return                           # the call will fail
            self._image(dst, link_ok=True)       # what the rename overwrites
            self._write(dict(k="moved", src=src, dst=dst))
            self._level.pop(src, None)
            self._level.pop(dst, None)
        elif event == "os.truncate":
            path = os.path.realpath(_abs(args[0]))
            st = _lstat(path)
            if st is not None and stat.S_ISREG(st.st_mode):
                grows = isinstance(args[1], int) and args[1] >= st.st_size
                self._content(path, 1 if grows else 2)
        elif event in ("os.chmod", "os.chown", "os.utime"):
            dir_fd = args[-1] if event == "os.chmod" and len(args) > 2 else None
            if event == "os.utime" and len(args) > 3:
                dir_fd = args[3]
            if event == "os.chown" and len(args) > 3:
                dir_fd = args[3]
            path = _abs(args[0], dir_fd)
            st = _lstat(path)
            if st is not None:
                self._write(dict(k="meta", path=path, mode=stat.S_IMODE(st.st_mode),
                                 uid=st.st_uid, gid=st.st_gid, at=st.st_atime_ns,
                                 mt=st.st_mtime_ns))
        elif event == "os.link":
            self._write(dict(k="absent", path=_abs(args[1], args[3] if len(args) > 3 else None)))
        elif event == "os.symlink":
            dst = _abs(args[1], args[2] if len(args) > 2 else None)
            if _lstat(dst) is None:
                self._write(dict(k="absent", path=dst))
        elif event == "sqlite3.connect":
            db = args[0] if args else None
            if isinstance(db, (str, bytes, os.PathLike)) and _fsdecode(db) not in ("", ":memory:") \
                    and not str(_fsdecode(db)).startswith("file:"):
                base = os.path.realpath(_abs(db))
                for suffix in ("", "-journal", "-wal", "-shm"):
                    self._content(base + suffix, 2)

    # ------------------------------------------------------------------
    # inherited descriptors (parent, before the fork)
    # ------------------------------------------------------------------
    def mark_fds(self):
        """Remember size (and small contents) of the regular files this process holds open for
        writing; the child shares them and can change them without any audit event."""
        self._fds = []
        try:
            names = os.listdir("/proc/self/fd")
        except OSError:
            return
        for name in names:
            try:
                fd = int(name)
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    continue
                flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                if flags & os.O_ACCMODE == os.O_RDONLY:
                    continue
                entry = dict(fd=fd, size=st.st_size, append=bool(flags & os.O_APPEND),
                             backup=None, at=st.st_atime_ns, mt=st.st_mtime_ns)
                if st.st_size <= FD_COPY and self._bytes + st.st_size <= self.max_total:
                    dest = os.path.join(self.dir, f"fd{fd}")
                    with open(f"/proc/self/fd/{fd}", "rb") as src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)
                    entry["backup"] = dest
                    self._bytes += st.st_size
                self._fds.append(entry)
            except OSError:
                continue

    def undo_fds(self) -> Tuple[List[str], List[str]]:
        done, failed = [], []
        for e in self._fds:
            fd = e["fd"]
            try:
                st = os.fstat(fd)
                if e["backup"] is not None:
                    with open(e["backup"], "rb") as f:
                        old = f.read()
                    with open(f"/proc/self/fd/{fd}", "rb") as g:      # the fd may be write-only
                        cur = g.read()
                    if cur == old:
                        continue
                    os.ftruncate(fd, 0)
                    if old:
                        os.pwrite(fd, old, 0)
                    os.utime(fd, ns=(e["at"], e["mt"]))
                    done.append(f"fd {fd}: content restored")
                elif st.st_size > e["size"]:
                    os.ftruncate(fd, e["size"])
                    os.utime(fd, ns=(e["at"], e["mt"]))
                    done.append(f"fd {fd}: cut back to {e['size']} bytes")
                elif st.st_size != e["size"]:
                    failed.append(f"fd {fd}: file shrank; cannot restore (too large to copy)")
            except OSError as ex:
                if ex.errno != errno.EBADF:       # a descriptor that is gone changes nothing
                    failed.append(f"fd {fd}: {ex}")
        return done, failed

    # ------------------------------------------------------------------
    # undo (parent) and discard
    # ------------------------------------------------------------------
    def undo(self) -> Tuple[List[str], List[str]]:
        """Reverse everything recorded; returns (what was undone, what could not be)."""
        entries = []
        try:
            with open(self.manifest) as f:
                for line in f:
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        pass                      # a line cut off by the kill: op had not run
        except FileNotFoundError:
            pass
        done, failed = [], []
        for e in reversed(entries):
            try:
                msg = self._undo_one(e)
                if msg:
                    done.append(msg)
            except Exception as ex:               # report, keep going with the rest
                failed.append(f"{e.get('path') or e.get('dst')}: {type(ex).__name__}: {ex}")
        d2, f2 = self.undo_fds()
        self.discard()
        return done + d2, failed + f2

    def _undo_one(self, e):
        k = e["k"]
        if k == "absent":
            p = e["path"]
            st = _lstat(p)
            if st is None:
                return ""
            if stat.S_ISDIR(st.st_mode):
                os.rmdir(p)
            else:
                os.unlink(p)
            return f"removed {p}"
        if k == "file":
            p, b = e["path"], os.path.join(self.dir, e["b"])
            st = _lstat(p)
            if e["how"] == "link":
                if st is not None:
                    if stat.S_ISDIR(st.st_mode):
                        os.rmdir(p)
                    else:
                        os.unlink(p)
                os.link(b, p)
            else:
                if st is not None and stat.S_ISREG(st.st_mode):
                    with open(b, "rb") as src, open(p, "wb") as out:    # same inode
                        shutil.copyfileobj(src, out)
                else:
                    if st is not None:
                        if stat.S_ISDIR(st.st_mode):
                            os.rmdir(p)
                        else:
                            os.unlink(p)
                    shutil.copyfile(b, p)
            os.chmod(p, e["mode"])
            os.utime(p, ns=(e["at"], e["mt"]))
            return f"restored {p}"
        if k == "size":
            p = e["path"]
            st = _lstat(p)
            if st is not None and stat.S_ISREG(st.st_mode) and st.st_size > e["size"]:
                os.truncate(p, e["size"])
                os.utime(p, ns=(e["at"], e["mt"]))
                return f"cut {p} back to {e['size']} bytes"
            return ""
        if k == "symlink":
            p = e["path"]
            if _lstat(p) is not None:
                os.unlink(p)
            os.symlink(e["target"], p)
            return f"restored link {p}"
        if k == "dir":
            p = e["path"]
            if _lstat(p) is None:
                os.mkdir(p)
            os.chmod(p, e["mode"])
            os.utime(p, ns=(e["at"], e["mt"]))
            return f"restored directory {p}"
        if k == "moved":
            if _lstat(e["src"]) is None and _lstat(e["dst"]) is not None:
                os.rename(e["dst"], e["src"])
                return f"moved {e['dst']} back to {e['src']}"
            return ""
        if k == "meta":
            p = e["path"]
            st = _lstat(p)
            if st is None:
                return ""
            if (st.st_uid, st.st_gid) != (e["uid"], e["gid"]):
                os.chown(p, e["uid"], e["gid"])
            os.chmod(p, e["mode"])
            os.utime(p, ns=(e["at"], e["mt"]))
            return f"restored attributes of {p}"
        raise ValueError(f"unknown journal entry {k}")

    def discard(self):
        shutil.rmtree(self.dir, ignore_errors=True)