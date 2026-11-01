"""Small native OS adapters shared by CLI, TUI and supervisor."""

import platform
from contextlib import contextmanager


def windows():
    return platform.system() == "Windows"


def os_name():
    if platform.system() == "Linux":
        return platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
    return platform.system()


@contextmanager
def file_lock(file):
    if windows():
        import msvcrt

        file.seek(0)
        if not file.read(1):
            file.write("0")
            file.flush()
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        yield
    finally:
        if windows():
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(file, fcntl.LOCK_UN)
