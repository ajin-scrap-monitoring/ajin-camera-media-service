import json
from pathlib import Path

import pytest

from camera_media_service.app import create_app
from camera_media_service.settings import Settings, SettingsError
from camera_media_service.storage_mount import MountRecord, StorageMountError, StorageMountGuard


def fixture_guard(tmp_path, **kwargs):
    root = tmp_path / "media"
    root.mkdir()
    (root / ".camera-media-filesystem.json").write_text(json.dumps({"uuid": "sd-uuid"}))
    mounts = [MountRecord(41, "179:1", root, frozenset({"rw"}), "ext4")]
    guard = StorageMountGuard(
        root,
        "sd-uuid",
        "179:1",
        read_mounts=lambda: mounts,
        device_of=lambda path: "179:1",
        **kwargs,
    )
    return guard, root, mounts


def test_guard_disabled_allows_missing_development_root(tmp_path):
    guard = StorageMountGuard(tmp_path / "missing")
    guard.check()
    assert not guard.root.exists()


def test_guard_accepts_mount_root_and_directory_beneath_mount(tmp_path):
    guard, root, mounts = fixture_guard(tmp_path)
    guard.check()
    child = root / "subdirectory"
    child.mkdir()
    StorageMountGuard(
        child,
        "sd-uuid",
        "179:1",
        identity_path=root,
        read_mounts=lambda: mounts,
        device_of=lambda path: "179:1",
    ).check()


@pytest.mark.parametrize("problem", ["missing", "wrong_uuid", "wrong_device", "readonly"])
def test_guard_rejects_unavailable_or_wrong_mount(tmp_path, problem):
    guard, root, mounts = fixture_guard(tmp_path)
    if problem == "missing":
        mounts.clear()
    elif problem == "wrong_uuid":
        (root / ".camera-media-filesystem.json").write_text('{"uuid":"other"}')
    elif problem == "wrong_device":
        mounts[0] = MountRecord(41, "8:1", root, frozenset({"rw"}), "ext4")
    else:
        mounts[0] = MountRecord(41, "179:1", root, frozenset({"ro"}), "ext4")
    with pytest.raises(StorageMountError):
        guard.check()


def test_guard_rejects_nonwritable_storage(tmp_path, monkeypatch):
    guard, _, _ = fixture_guard(tmp_path)
    monkeypatch.setattr("camera_media_service.storage_mount.os.access", lambda *a, **k: False)
    with pytest.raises(StorageMountError, match="writ"):
        guard.check()


def test_runtime_remount_latches_failure_until_service_restart(tmp_path):
    guard, root, mounts = fixture_guard(tmp_path)
    guard.check()
    mounts[0] = MountRecord(42, "179:1", root, frozenset({"rw"}), "ext4")
    with pytest.raises(StorageMountError, match="changed"):
        guard.check()
    mounts[0] = MountRecord(41, "179:1", root, frozenset({"rw"}), "ext4")
    with pytest.raises(StorageMountError):
        guard.check()


def test_host_witness_detects_detached_docker_bind(tmp_path):
    guard, root, mounts = fixture_guard(tmp_path)
    witness = tmp_path / "host-media"
    witness.mkdir()
    guard.witness_path = witness
    mounts.append(
        MountRecord(51, "179:1", witness, frozenset({"ro"}), "ext4", frozenset({"master:1"}))
    )
    guard.check()
    mounts.pop()  # root bind still pins SD, host's SD submount has disappeared
    with pytest.raises(StorageMountError):
        guard.check()


def test_host_witness_refuses_private_unpropagated_mount(tmp_path):
    guard, _, mounts = fixture_guard(tmp_path)
    witness = tmp_path / "host-media"
    witness.mkdir()
    guard.witness_path = witness
    mounts.append(MountRecord(51, "179:1", witness, frozenset({"ro"}), "ext4"))
    with pytest.raises(StorageMountError, match="propagat"):
        guard.check()


def test_settings_require_complete_mount_identity():
    with pytest.raises(SettingsError):
        Settings.from_env({"EDGE_AUTH_SECRET": "{}", "MEDIA_EXPECTED_FS_UUID": "sd-uuid"})


def test_create_app_checks_guard_before_creating_missing_root(tmp_path):
    root = tmp_path / "missing"
    settings = Settings(
        camera_tokens={},
        media_root=str(root),
        media_expected_fs_uuid="sd-uuid",
        media_expected_device="179:1",
    )
    with pytest.raises(StorageMountError):
        create_app(settings)
    assert not root.exists()


def test_mountinfo_parser_decodes_paths_and_combines_superblock_readonly():
    from camera_media_service.storage_mount import parse_mountinfo

    rows = parse_mountinfo("41 1 179:1 / /media\\040disk rw - ext4 /dev/mmcblk0p1 ro\n")
    assert rows == [MountRecord(41, "179:1", Path("/media disk"), frozenset({"rw", "ro"}), "ext4")]
