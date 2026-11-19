"""Candados no bloqueantes compartidos por CLI, Kick y resolución manual."""
from __future__ import annotations

import errno
import sys
from contextlib import ExitStack
from pathlib import Path


def acquire(stream):
    if sys.platform == "win32":
        import msvcrt
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            raise BlockingIOError("Ya hay otro proceso de canje corriendo") from exc
    else:
        import fcntl
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BlockingIOError("Ya hay otro proceso de canje corriendo") from exc


def release(stream):
    if sys.platform == "win32":
        import msvcrt
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream, fcntl.LOCK_UN)


class FileLock:
    def __init__(self, path, *, readonly=False):
        self.path = Path(path)
        self.readonly = readonly
        self.stream = None

    def __enter__(self):
        self.stream = self.path.open("rb" if self.readonly else "a+b")
        try:
            acquire(self.stream)
        except BaseException:
            self.stream.close()
            self.stream = None
            raise
        return self

    def close(self):
        if self.stream is not None:
            try:
                release(self.stream)
            finally:
                self.stream.close()
                self.stream = None

    def __exit__(self, *exc):
        self.close()


def global_directory(directory):
    directory = Path(directory)
    return directory.parent if directory.name.startswith("kick-") else directory


def lock_directory(directory, *, include_global=True):
    directory = Path(directory)
    stack = ExitStack()
    paths = [global_directory(directory)] if include_global else []
    if directory not in paths:
        paths.append(directory)
    try:
        for folder in paths:
            folder.mkdir(parents=True, exist_ok=True)
            stack.enter_context(FileLock(folder / ".redeemer.lock"))
    except BaseException:
        stack.close()
        raise
    return stack
