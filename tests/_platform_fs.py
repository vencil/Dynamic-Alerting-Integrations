"""What a filesystem call raises when its target is a directory, per OS (#2557).

POSIX reports writing to (or ``shutil.copy2`` from) a directory as EISDIR,
i.e. ``IsADirectoryError``. Windows reports the same call as EACCES, i.e.
``PermissionError``. A test that pins the POSIX type goes red on a Windows
host although the tool behaved correctly (rc 2, one line naming the path).

Each constant is ONE value per platform, not a union: widening the Linux
side to ``(IsADirectoryError, PermissionError)`` would let a real permission
failure pass as the directory case there, and Linux CI is where these tests
carry weight.
"""
import errno
import os

#: The exception type opening a directory for writing raises on this OS.
DIR_WRITE_ERROR = PermissionError if os.name == "nt" else IsADirectoryError

#: ``strerror`` text a tool echoes for that failure on this OS.
DIR_WRITE_STRERROR = os.strerror(errno.EACCES if os.name == "nt" else errno.EISDIR)
