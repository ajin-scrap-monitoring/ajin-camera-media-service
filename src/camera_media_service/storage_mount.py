"""Fail closed on configured removable-media identity or mount changes.

The UUID marker is corroborating identity, not a substitute for the kernel device.
The expected major:minor must be obtained from the UUID's block device on the host.
Docker deployments additionally use a propagated host-parent mount witness so a
detached but still pinned media bind cannot masquerade as a healthy host mount.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MARKER_NAME = ".camera-media-filesystem.json"


class StorageMountError(OSError):
    """The configured storage is unavailable; never create a replacement root."""


@dataclass(frozen=True)
class MountRecord:
    mount_id: int
    device: str
    path: Path
    options: frozenset[str]
    filesystem: str
    propagation: frozenset[str] = frozenset()


def parse_mountinfo(text: str) -> list[MountRecord]:
    records = []
    for line in text.splitlines():
        before, after = line.split(" - ", 1)
        fields, filesystem = before.split(), after.split()
        path = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])
        records.append(
            MountRecord(
                int(fields[0]),
                fields[2],
                Path(path),
                frozenset(fields[5].split(",") + filesystem[2].split(",")),
                filesystem[0],
                frozenset(fields[6:]),
            )
        )
    return records


def _read_mounts() -> list[MountRecord]:
    return parse_mountinfo(Path("/proc/self/mountinfo").read_text())


def _device_of(path: Path) -> str:
    device = path.stat().st_dev
    return f"{os.major(device)}:{os.minor(device)}"


class StorageMountGuard:
    def __init__(
        self,
        root: Path,
        expected_uuid: str = "",
        expected_device: str = "",
        *,
        identity_path: Path | None = None,
        witness_path: Path | None = None,
        read_mounts: Callable[[], list[MountRecord]] = _read_mounts,
        device_of: Callable[[Path], str] = _device_of,
    ) -> None:
        self.root = root.absolute()
        self.expected_uuid = expected_uuid
        self.expected_device = expected_device
        self.identity_path = (identity_path or root).absolute()
        self.witness_path = witness_path.absolute() if witness_path else None
        self._read_mounts = read_mounts
        self._device_of = device_of
        self._mount_ids: tuple[int, ...] | None = None
        self._failed = False

    @classmethod
    def from_settings(cls, settings) -> StorageMountGuard:
        return cls(
            Path(settings.media_root),
            settings.media_expected_fs_uuid,
            settings.media_expected_device,
            identity_path=Path(settings.media_identity_path)
            if settings.media_identity_path
            else None,
            witness_path=Path(settings.media_mount_witness_path)
            if settings.media_mount_witness_path
            else None,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.expected_uuid or self.expected_device)

    def check(self) -> None:
        if not self.enabled:
            return
        if self._failed:
            raise StorageMountError("Media mount changed or unavailable; restart required")
        try:
            self._check()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._failed = True
            if isinstance(exc, StorageMountError):
                raise
            raise StorageMountError("Media mount identity unavailable") from exc

    def _check(self) -> None:
        if not self.expected_uuid or not re.fullmatch(r"\d+:\d+", self.expected_device):
            raise StorageMountError("Media mount requires UUID and kernel device identity")
        for path in (self.root, self.identity_path):
            if not path.is_dir() or path.resolve() != path:
                raise StorageMountError("Media path missing or contains a symlink")
        mounts = self._read_mounts()
        ids = []
        paths = [self.root, self.identity_path]
        if self.witness_path is not None:
            paths.append(self.witness_path)
        for path in paths:
            matching = [m for m in mounts if path == m.path or m.path in path.parents]
            if not matching:
                raise StorageMountError("Media mount missing")
            mount = max(matching, key=lambda m: len(m.path.parts))
            if mount.device != self.expected_device or self._device_of(path) != mount.device:
                raise StorageMountError("Media mount kernel device mismatch")
            if mount.filesystem != "ext4":
                raise StorageMountError("Media mount must use ext4")
            if path == self.witness_path:
                if mount.path != path:
                    raise StorageMountError("Propagated host media mount missing")
                if not any(option.startswith("master:") for option in mount.propagation):
                    raise StorageMountError("Host media witness lacks slave mount propagation")
            elif "ro" in mount.options or "rw" not in mount.options:
                raise StorageMountError("Media mount is read-only")
            ids.append(mount.mount_id)
        mount_ids = tuple(ids)
        if self._mount_ids is not None and mount_ids != self._mount_ids:
            raise StorageMountError("Media mount changed; restart required")
        marker = self.identity_path / MARKER_NAME
        if marker.is_symlink() or marker.stat().st_size > 4096:
            raise StorageMountError("Invalid media filesystem identity marker")
        if json.loads(marker.read_text())["uuid"] != self.expected_uuid:
            raise StorageMountError("Media filesystem UUID marker mismatch")
        if not os.access(self.root, os.W_OK | os.X_OK):
            raise StorageMountError("Media root is not writable")
        self._mount_ids = mount_ids
