"""Descriptor-bound operations for the Linux folder worker."""

import ctypes
import errno
import fcntl
import os
import shutil
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import uuid4


_STAGE = ".document-ingestion"
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_RENAME_NOREPLACE = 1


def absolute(path):
    return Path(os.path.abspath(Path(path).expanduser()))


@contextmanager
def directory(path, *, create=False):
    """Open each directory component without following symbolic links."""
    path = absolute(path)
    fd = os.open(path.anchor, _DIR)
    try:
        for component in path.parts[1:]:
            if create:
                try:
                    os.mkdir(component, mode=0o750, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(component, _DIR, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _stage_dir(parent, *, create):
    if create:
        try:
            os.mkdir(_STAGE, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(_STAGE, _DIR, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError("The folder worker requires a private staging directory.")
        yield fd
    finally:
        os.close(fd)


def _entries(fd):
    entries = {}
    for name in os.listdir(fd):
        if name.startswith("."):
            continue
        try:
            entries[name] = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
    return entries


def entries(fd):
    """Include unacknowledged inputs left by a stopped worker."""
    found = _entries(fd)
    try:
        with _stage_dir(fd, create=False) as stage:
            found.update(_entries(stage))
    except FileNotFoundError:
        pass
    return sorted(found.items(), key=lambda entry: entry[0].lower())


def _rename_new(source, name, destination, new_name):
    # Linux renameat2(RENAME_NOREPLACE) atomically reserves the destination.
    rename = ctypes.CDLL(None, use_errno=True).renameat2
    if rename(source, os.fsencode(name), destination, os.fsencode(new_name), _RENAME_NOREPLACE):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), name)


def _take(parent, name, stage, staged_name, expected):
    """Claim a directory entry atomically and verify its identity before use."""
    _rename_new(parent, name, stage, staged_name)
    actual = os.stat(staged_name, dir_fd=stage, follow_symlinks=False)
    if stat.S_IFMT(actual.st_mode) == stat.S_IFMT(expected.st_mode) and os.path.samestat(expected, actual):
        return
    try:
        _rename_new(stage, staged_name, parent, name)
    except FileExistsError:
        # A new arrival owns the original name. Retain the displaced entry privately.
        _rename_new(stage, staged_name, stage, ".changed-" + uuid4().hex)
    raise OSError(errno.ESTALE, "The input changed before it could be claimed.", name)


def _output_names(filename):
    path = Path(filename)
    yield filename
    for index in range(1, 1000):
        yield f"{path.stem}-{index}{path.suffix}"


def publish(stream, target, filename, pins=None):
    """Publish a complete copy; an existing file, directory or link is never replaced."""
    with directory(target, create=True) as out, _stage_dir(out, create=True) as private:
        temporary = ".output-" + uuid4().hex
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=private)
        try:
            with os.fdopen(fd, "wb") as output:
                stream.seek(0)
                shutil.copyfileobj(stream, output)
                identity = os.fstat(output.fileno())
                if pins is not None:
                    # Retain the copied inode until rollback or acknowledgement.
                    pins.callback(os.close, os.dup(output.fileno()))
            for name in _output_names(filename):
                try:
                    _rename_new(private, temporary, out, name)
                except FileExistsError:
                    continue
                return target / name, identity
            raise FileExistsError("No unused output filename is available.")
        finally:
            try:
                os.unlink(temporary, dir_fd=private)
            except FileNotFoundError:
                pass


def retract(path, identity):
    """Roll back a publication only when the entry is the one we wrote."""
    with directory(path.parent) as out, _stage_dir(out, create=True) as private:
        name = ".rollback-" + uuid4().hex
        _take(out, path.name, private, name, identity)
        os.unlink(name, dir_fd=private)


class ClaimedFile:
    def __init__(self, path, stream, stage, pins):
        self.path = path
        self.stream = stream
        self.stage = stage
        self.pins = pins

    def publish(self, target):
        fd = self.stream.fileno()
        identity = os.fstat(fd)
        with directory(target, create=True) as out:
            if identity.st_dev == os.fstat(out).st_dev:
                for name in _output_names(self.path.name):
                    try:
                        # Keep the opened regular inode and its metadata on the same filesystem.
                        os.link(f"/proc/self/fd/{fd}", name, dst_dir_fd=out, follow_symlinks=True)
                    except FileExistsError:
                        continue
                    except OSError as exc:
                        if exc.errno in {errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP, errno.EMLINK}:
                            break
                        raise
                    return target / name, identity
                else:
                    raise FileExistsError("No unused output filename is available.")
        return publish(self.stream, target, self.path.name, self.pins)

    def discard(self):
        name = ".completed-" + uuid4().hex
        _take(self.stage, self.path.name, self.stage, name, os.fstat(self.stream.fileno()))
        os.unlink(name, dir_fd=self.stage)


@contextmanager
def regular_file(parent, name, expected=None):
    expected = expected or os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISREG(expected.st_mode):
        raise ValueError("The input must be a regular file.")
    # O_PATH identifies the entry without opening a FIFO or device. Reopen that pinned
    # regular inode, not its writable directory entry.
    fd = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=parent)
    try:
        actual = os.fstat(fd)
        if not stat.S_ISREG(actual.st_mode) or not os.path.samestat(actual, expected):
            raise OSError(errno.ESTALE, "The input changed before reading.", name)
        with open(f"/proc/self/fd/{fd}", "rb") as stream:
            yield stream
    finally:
        os.close(fd)


@contextmanager
def claimed_file(root, path):
    path = absolute(path)
    relative = path.relative_to(absolute(root))
    if not 1 <= len(relative.parts) <= 2 or any(part.startswith(".") for part in relative.parts):
        raise ValueError("The input must be in the folder or its mapped category.")
    with directory(path.parent) as parent, _stage_dir(parent, create=True) as stage:
        fcntl.flock(stage, fcntl.LOCK_EX)
        try:
            expected = os.stat(path.name, dir_fd=stage, follow_symlinks=False)
        except FileNotFoundError:
            expected = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(expected.st_mode):
                raise ValueError("The input must be a regular file.")
            _take(parent, path.name, stage, path.name, expected)
        with regular_file(stage, path.name, expected) as stream, ExitStack() as pins:
            yield ClaimedFile(path, stream, stage, pins)
