"""Exact physical file accounting without allocating a Path for every entry."""

import os
import stat
from os import scandir


def iter_regular_files(root, *, exclude=(), category=None):
    stack = [(os.fspath(root), category)]
    while stack:
        folder, first = stack.pop()
        try:
            with scandir(folder) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if first is not None or entry.name not in exclude:
                                stack.append((entry.path, first or entry.name))
                            continue
                        info = entry.stat()
                    except FileNotFoundError:
                        # Retention/compaction can remove a scanned entry.
                        continue
                    if stat.S_ISREG(info.st_mode):
                        yield entry.path, info, first
        except FileNotFoundError:
            continue
