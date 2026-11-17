"""Linux descriptor-relative pressure cleanup; never follow links or other mounts."""

import os
import shutil
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path


def _directory(parent, name, device=None):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    info = os.fstat(fd)
    if device is not None and info.st_dev != device:
        os.close(fd)
        raise OSError("Pressure cleanup crossed a filesystem")
    return fd


def _scan(fd, device, *, depth=0, count=None):
    if count is None:
        count = [0]
    if depth > 8:
        raise OSError("Pressure cleanup tree too deep")
    total = 0
    for name in os.listdir(fd):
        count[0] += 1
        if count[0] > 100000:
            raise OSError("Pressure cleanup tree too large")
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if info.st_dev != device:
            raise OSError("Pressure cleanup crossed a filesystem")
        if stat.S_ISDIR(info.st_mode):
            child = _directory(fd, name, device)
            try:
                total += _scan(child, device, depth=depth + 1, count=count)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            total += info.st_size
        else:
            raise OSError("Pressure cleanup refuses linked or special files")
    return total


@contextmanager
def _assets(root, key, guard):
    if not shutil.rmtree.avoids_symlink_attacks:
        raise OSError("Safe pressure deletion requires Linux directory descriptors")
    if str(uuid.UUID(key)) != key:
        raise OSError("Invalid owned day identifier")
    root = Path(root).absolute()
    if root.resolve() != root:
        raise OSError("Pressure cleanup root contains a link")
    guard()
    rootfd = _directory(None, str(root))
    device = os.fstat(rootfd).st_dev
    opened = []
    assets = []
    total = 0
    try:
        for category, names, folder in (
            ("daily", (key,), True),
            ("daily-playback", (key,), True),
            ("daily-exports", tuple(key + s for s in (".mp4", ".tmp.mp4", ".ffconcat")), False),
            ("daily-thumbnails", (key + ".jpg",), False),
        ):
            try:
                fd = _directory(rootfd, category, device)
            except FileNotFoundError:
                continue
            opened.append(fd)
            for name in names:
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if info.st_dev != device:
                    raise OSError("Pressure cleanup crossed a filesystem")
                if folder and stat.S_ISDIR(info.st_mode):
                    child = _directory(fd, name, device)
                    try:
                        total += _scan(child, device)
                    finally:
                        os.close(child)
                elif not folder and stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    total += info.st_size
                else:
                    raise OSError("Pressure cleanup refuses linked or special assets")
                assets.append((fd, name, folder, info.st_ino))
        guard()
        yield total, assets
    finally:
        for fd in opened:
            os.close(fd)
        os.close(rootfd)


def inspect(root, key, guard):
    with _assets(root, key, guard) as (total, _):
        return {"bytes": total}


def remove(root, key, guard):
    with _assets(root, key, guard) as (_, assets):
        for fd, name, folder, inode in assets:
            guard()
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if info.st_ino != inode:
                raise OSError("Pressure cleanup asset changed")
            if folder:
                shutil.rmtree(name, dir_fd=fd)
            else:
                os.unlink(name, dir_fd=fd)
        guard()
