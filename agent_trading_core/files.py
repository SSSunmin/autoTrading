"""Files written whole, and held by one writer at a time, under concurrent writers."""

import errno
import logging
import os
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

# Windows refuses the move (WinError 5) while a scanner or an indexer has the file just read or written open:
# 13 ms until it went through, 1 refusal in 18,020 moves under load (flaky-sqlite). Tried again for 0.5 s at most.
REPLACE_TRIES, REPLACE_WAIT = 10, 0.05


def replace_file(path, write: Callable[[str], None]) -> None:
    """Write ``path`` through a uniquely named temp file beside it, then move it into place.

    A reader sees the old file or the new one, never a partial write, and two
    writers of the same path (tool calls run concurrently) never share a temp file.
    ``write`` receives the temp path and creates the file, so it gets the usual
    permissions. The file is a cache: where another reader holds it open (Windows)
    past ``REPLACE_TRIES`` moves, the old one stays and the write is skipped.
    """
    path = Path(path)
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        write(str(temp))
        for attempt in range(REPLACE_TRIES):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if attempt == REPLACE_TRIES - 1:
                    raise
                time.sleep(REPLACE_WAIT)
    except PermissionError as exc:
        temp.unlink(missing_ok=True)
        logger.warning("Kept the cached %s; it is in use (%s)", path.name, exc)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


@contextmanager
def locked(path, wait: bool = True) -> Iterator[bool]:
    """Hold ``path`` for one writer at a time, across threads and processes.

    For a file read, changed and written back: without the lock, two writers
    each read the same text and the second write drops the first's change. The
    lock is taken on a ``.lock`` file beside ``path``. Yields whether it is
    held: with ``wait=False`` a lock someone else holds is not waited for, and
    the block runs holding nothing.
    """
    with open(f"{path}.lock", "a+b") as handle:
        hold = _hold_windows if os.name == "nt" else _hold_posix
        with hold(handle, wait) as acquired:
            yield acquired


@contextmanager
def _hold_posix(handle, wait: bool = True) -> Iterator[bool]:
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _hold_windows(handle, wait: bool = True) -> Iterator[bool]:
    import msvcrt

    handle.seek(0)
    while True:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)
            break
        except OSError as exc:
            if not wait and exc.errno in (errno.EACCES, errno.EDEADLK):
                yield False   # another writer holds it
                return
            # LK_LOCK gives up after about ten seconds of another writer's hold;
            # any other failure is not a wait.
            if exc.errno != errno.EDEADLK:
                raise
    try:
        yield True
    finally:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
