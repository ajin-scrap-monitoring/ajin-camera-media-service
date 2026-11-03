#!/usr/bin/env python3
"""Copy-only Linux media migration. Never stops services, moves, or deletes footage.

Run from the repository with PYTHONPATH=src. Manifest paths are local evidence;
stdout contains only aggregate counts and filesystem identity, never environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath

from camera_media_service.storage_mount import MARKER_NAME, StorageMountGuard, parse_mountinfo

STAGING_NAME = ".camera-media-migration-staging"


class MigrationError(RuntimeError):
    pass


def _root(path: Path) -> Path:
    absolute = path.absolute()
    if absolute.resolve() != absolute or not absolute.is_dir() or absolute == Path(absolute.anchor):
        raise MigrationError("Exact existing non-symlink storage directory required")
    return absolute


def _overlaps(left: Path, right: Path) -> bool:
    """Detect lexical overlap AND multiple bind/mount aliases of the same subtree."""
    if left == right or left in right.parents or right in left.parents:
        return True

    def identity(path):
        info = path.stat()
        return info.st_dev, info.st_ino

    try:
        left_identity, right_identity = identity(left), identity(right)
        return left_identity in {
            identity(p) for p in (right, *right.parents)
        } or right_identity in {identity(p) for p in (left, *left.parents)}
    except FileNotFoundError:
        return False


def _separate(source: Path, target: Path) -> tuple[Path, Path]:
    source, target = _root(source), _root(target)
    if _overlaps(source, target):
        raise MigrationError("Source and target must be disjoint directories")
    return source, target


@contextmanager
def _directory(root: Path, relative: str = "", *, create: bool = False):
    """Pin each ancestor with O_NOFOLLOW; no path-based privileged destination writes."""
    parts = PurePosixPath(relative).parts if relative else ()
    if any(part in ("..", "/") for part in parts):
        raise MigrationError("Invalid relative path")
    if os.name != "posix":  # Testable copy core; production CLI refuses non-Linux.
        current = root
        for part in parts:
            current = current / part
            if create and not current.exists():
                current.mkdir()
            if current.is_symlink() or not current.is_dir():
                raise MigrationError("Directory symlink or missing ancestor")
        yield current
        return
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    device = os.fstat(descriptor).st_dev
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            if os.fstat(child).st_dev != device:
                os.close(child)
                raise MigrationError("Nested filesystem in storage tree")
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _file(root: Path, relative: str):
    path = PurePosixPath(relative)
    with _directory(root, str(path.parent) if str(path.parent) != "." else "") as parent:
        if isinstance(parent, int):
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise MigrationError("Nonregular storage file")
            with os.fdopen(descriptor, "rb") as stream:
                yield stream
        else:
            full = parent / path.name
            if not full.exists() and not full.is_symlink():
                raise FileNotFoundError(full)
            if full.is_symlink() or not full.is_file():
                raise MigrationError("Storage symlink or nonregular file")
            with full.open("rb") as stream:
                yield stream


def _metadata(info) -> dict:
    return {"mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid}


def _sqlite_summary(root: Path, relative: str) -> dict:
    # Integrity checking only a disposable copy keeps source/WAL/SHM byte-for-byte untouched.
    with tempfile.TemporaryDirectory(prefix="media-sqlite-check-") as scratch:
        database = Path(scratch) / "catalog.sqlite3"
        for suffix in ("", "-wal", "-shm"):
            try:
                with _file(root, relative + suffix) as source:
                    with Path(str(database) + suffix).open("wb") as target:
                        shutil.copyfileobj(source, target)
            except FileNotFoundError:
                if not suffix:
                    raise
        with closing(sqlite3.connect(database)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise MigrationError("SQLite integrity check failed")
            names = connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {}
            for (name,) in names.fetchall():
                quoted = '"' + name.replace('"', '""') + '"'
                tables[name] = connection.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]
            return {"integrity": "ok", "tables": tables}


def build_manifest(root: Path, *, target: bool = False, check_sqlite: bool = True) -> dict:
    root = _root(root)
    files, directories, sqlite = [], [], {}
    root_device = root.stat().st_dev

    def visit(relative=""):
        with _directory(root, relative) as directory:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
            for entry in entries:
                name = f"{relative}/{entry.name}" if relative else entry.name
                if name == MARKER_NAME:
                    continue
                if name == STAGING_NAME:
                    if not target:
                        raise MigrationError("Source contains reserved migration staging directory")
                    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                        raise MigrationError("Invalid migration staging directory")
                    continue
                info = entry.stat(follow_symlinks=False)
                if os.name != "posix":
                    info = (root / name).lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise MigrationError("Storage symlink refused")
                if info.st_dev != root_device:
                    raise MigrationError("Nested filesystem refused")
                if stat.S_ISDIR(info.st_mode):
                    directories.append({"path": name, **_metadata(info)})
                    visit(name)
                elif stat.S_ISREG(info.st_mode):
                    with _file(root, name) as stream:
                        before = os.fstat(stream.fileno())
                        header = stream.read(16)
                        stream.seek(0)
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                        after = os.fstat(stream.fileno())

                    def fingerprint(s):
                        return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns

                    if fingerprint(before) != fingerprint(after):
                        raise MigrationError("Source changed while hashing")
                    files.append(
                        {
                            "path": name,
                            "size": before.st_size,
                            "mtime_ns": before.st_mtime_ns,
                            "sha256": digest,
                            **_metadata(before),
                        }
                    )
                    if check_sqlite and header == b"SQLite format 3\x00":
                        sqlite[name] = _sqlite_summary(root, name)
                else:
                    raise MigrationError("Nonregular storage entry refused")

    visit()
    return {
        "version": 1,
        "root": _metadata(root.stat()),
        "directories": directories,
        "files": files,
        "sqlite": sqlite,
    }


def _apply_metadata(descriptor, info: dict):
    if isinstance(descriptor, int):
        os.fchown(descriptor, info["uid"], info["gid"])
        os.fchmod(descriptor, info["mode"])
        if "mtime_ns" in info:
            os.utime(descriptor, ns=(info["mtime_ns"], info["mtime_ns"]))
    else:
        os.chmod(descriptor, info["mode"])
        if "mtime_ns" in info:
            os.utime(descriptor, ns=(info["mtime_ns"], info["mtime_ns"]))


def _copy_file(source: Path, target: Path, info: dict):
    path = PurePosixPath(info["path"])
    relative_parent = str(path.parent) if str(path.parent) != "." else ""
    with (
        _directory(target, relative_parent, create=True) as directory,
        _directory(target, STAGING_NAME, create=True) as staging,
    ):
        temporary = f".media-copy-{uuid.uuid4().hex}"
        if isinstance(directory, int):
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=staging,
            )
            try:
                with os.fdopen(descriptor, "wb") as output:
                    with _file(source, info["path"]) as input_file:
                        shutil.copyfileobj(input_file, output, length=1024 * 1024)
                    output.flush()
                    _apply_metadata(output.fileno(), info)
                    os.fsync(output.fileno())
                # link is an atomic no-clobber publication. Never replace a destination file.
                os.link(
                    temporary,
                    path.name,
                    src_dir_fd=staging,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
                os.fsync(directory)
            finally:
                os.unlink(temporary, dir_fd=staging)  # Only the exact temp file created here.
        else:
            temporary_path = staging / temporary
            try:
                with temporary_path.open("xb") as output:
                    with _file(source, info["path"]) as input_file:
                        shutil.copyfileobj(input_file, output)
                _apply_metadata(temporary_path, info)
                os.link(temporary_path, directory / path.name)
            finally:
                temporary_path.unlink(missing_ok=True)


def copy_storage(source: Path, target: Path, *, quiescent: Callable, guard: Callable) -> dict:
    source, target = _separate(source, target)
    quiescent()
    guard()
    manifest = build_manifest(source)
    destination = build_manifest(target, target=True, check_sqlite=False)
    expected = {item["path"]: item for item in manifest["files"]}
    existing = {item["path"]: item for item in destination["files"]}
    if any(name not in expected or info != expected[name] for name, info in existing.items()):
        raise MigrationError("Destination mismatch; existing files are never overwritten")
    wanted_directories = {item["path"] for item in manifest["directories"]}
    if any(item["path"] not in wanted_directories for item in destination["directories"]):
        raise MigrationError("Destination has extra directories")
    for info in manifest["directories"]:
        guard()
        with _directory(target, info["path"], create=True):
            pass
    for name, info in expected.items():
        guard()
        if name not in existing:
            _copy_file(source, target, info)
    for info in reversed(manifest["directories"]):
        with _directory(target, info["path"]) as directory:
            _apply_metadata(directory, info)
    with _directory(target) as directory:
        _apply_metadata(directory, manifest["root"])
    verify_storage(source, target, manifest, quiescent=quiescent, guard=guard)
    return manifest


def verify_storage(
    source: Path, target: Path, manifest: dict, *, quiescent: Callable, guard: Callable
) -> None:
    source, target = _separate(source, target)
    quiescent()
    guard()
    if build_manifest(source) != manifest:
        raise MigrationError("Source changed since manifest; stop and investigate")
    if build_manifest(target, target=True) != manifest:
        raise MigrationError("Destination manifest mismatch")
    quiescent()
    guard()


def _command(arguments: list[str]) -> str:
    completed = subprocess.run(arguments, check=True, capture_output=True, text=True, timeout=30)
    return completed.stdout.strip()


def inspect_target(target: Path, expected_uuid: str, expected_parent: str) -> dict:
    target = _root(target)
    device = Path(_command(["blkid", "-U", expected_uuid]))
    info = device.stat()
    if not stat.S_ISBLK(info.st_mode) or target.stat().st_dev != info.st_rdev:
        raise MigrationError("Target does not reside on expected UUID block device")
    kernel_device = f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"
    ancestry = (Path("/sys/dev/block") / kernel_device).resolve(strict=True)
    if expected_parent not in [part.name for part in (ancestry, *ancestry.parents)]:
        raise MigrationError("Target block device ancestry mismatch")
    mounts = parse_mountinfo(Path("/proc/self/mountinfo").read_text())
    candidates = [m for m in mounts if m.path == target or m.path in target.parents]
    mount = max(candidates, key=lambda m: len(m.path.parts), default=None)
    if mount is None or mount.device != kernel_device or mount.filesystem != "ext4":
        raise MigrationError("Target ext4 mount missing")
    if "ro" in mount.options or not os.access(target, os.W_OK | os.X_OK):
        raise MigrationError("Target is not writable")
    filesystem = os.statvfs(target)
    return {
        "uuid": expected_uuid,
        "device": kernel_device,
        "capacity_bytes": filesystem.f_blocks * filesystem.f_frsize,
        "available_bytes": filesystem.f_bavail * filesystem.f_frsize,
    }


def assert_quiescent(source: Path, target: Path, container: str) -> None:
    stopped = json.loads(_command(["docker", "inspect", container]))[0]
    if stopped["State"]["Running"] or stopped["State"].get("Restarting"):
        raise MigrationError("Media container must remain stopped")
    if not any(Path(m["Source"]) == source for m in stopped.get("Mounts", [])):
        raise MigrationError("Stopped container does not reference the exact source")
    running = _command(["docker", "ps", "-q"]).splitlines()
    for identifier in running:
        description = json.loads(_command(["docker", "inspect", identifier]))[0]
        for mount in description.get("Mounts", []):
            path = Path(mount.get("Source", "/nonexistent"))
            if any(_overlaps(path, root) for root in (source, target)):
                raise MigrationError("Running container references migration storage")
    # Root host process scan complements Docker status (host writers, mmap-only DB users).
    for process in Path("/proc").iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            for entry in (process / "fd").iterdir():
                try:
                    path = Path(os.readlink(entry).removesuffix(" (deleted)"))
                except FileNotFoundError:
                    continue
                if any(_overlaps(path, root) for root in (source, target)):
                    raise MigrationError("Host process has migration storage open")
            for mapping in (process / "maps").read_text().splitlines():
                fields = mapping.split(maxsplit=5)
                if len(fields) == 6 and fields[5].startswith("/"):
                    path = Path(fields[5].removesuffix(" (deleted)"))
                    if any(_overlaps(path, root) for root in (source, target)):
                        raise MigrationError("Host process maps migration storage")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as exc:
            raise MigrationError("Cannot prove host process quiescence") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("inspect", "initialize", "copy", "verify"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--expected-uuid", required=True)
    parser.add_argument("--expected-parent", default="mmcblk0")
    parser.add_argument("--stopped-container")
    parser.add_argument("--manifest", type=Path)
    arguments = parser.parse_args()
    try:
        if not Path("/proc/self/mountinfo").exists() or os.geteuid() != 0:
            raise MigrationError("Run on the Linux Docker host as root")
        target = _root(arguments.target)
        inspection = inspect_target(target, arguments.expected_uuid, arguments.expected_parent)
        if arguments.operation == "inspect":
            print(json.dumps(inspection, sort_keys=True))
            return 0
        if arguments.operation == "initialize":
            with _directory(target) as descriptor:
                fd = os.open(
                    MARKER_NAME,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o644,
                    dir_fd=descriptor,
                )
                with os.fdopen(fd, "w") as output:
                    json.dump({"uuid": arguments.expected_uuid}, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.fsync(descriptor)
            print(json.dumps({"initialized": True, **inspection}))
            return 0
        if not arguments.source or not arguments.stopped_container or not arguments.manifest:
            raise MigrationError("copy/verify require source, stopped-container and manifest")
        source, target = _separate(arguments.source, target)
        evidence = arguments.manifest.absolute()
        if evidence.parent.resolve() != evidence.parent:
            raise MigrationError("Manifest parent must not traverse symlinks")
        if any(evidence == root or root in evidence.parents for root in (source, target)):
            raise MigrationError("Manifest must be outside both storage trees")
        guard = StorageMountGuard(target, arguments.expected_uuid, inspection["device"])

        def quiescent():
            assert_quiescent(source, target, arguments.stopped_container)

        if arguments.operation == "copy":
            if evidence.exists():
                raise MigrationError("Manifest already exists; use a fresh external evidence path")
            manifest = copy_storage(source, target, quiescent=quiescent, guard=guard.check)
            descriptor = os.open(
                evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "w") as output:
                json.dump(manifest, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
        else:
            manifest = json.loads(evidence.read_text())
            verify_storage(source, target, manifest, quiescent=quiescent, guard=guard.check)
        print(
            json.dumps(
                {
                    "verified": True,
                    "files": len(manifest["files"]),
                    "bytes": sum(f["size"] for f in manifest["files"]),
                    "sqlite_databases": len(manifest["sqlite"]),
                    **inspection,
                }
            )
        )
        return 0
    except (OSError, ValueError, MigrationError, sqlite3.Error, subprocess.SubprocessError) as exc:
        # Do not print Docker inspect/env, credentials, filenames, or DB row values.
        reason = (
            str(exc)
            if isinstance(exc, MigrationError)
            else "migration refused; inspect local state"
        )
        print(json.dumps({"verified": False, "error": reason}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
