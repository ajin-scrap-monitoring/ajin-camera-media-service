import importlib.util
import shutil
import uuid

import pytest


def module():
    assert importlib.util.find_spec("camera_media_service.pressure_files"), (
        "safe pressure file operations missing"
    )
    from camera_media_service import pressure_files

    return pressure_files


def test_pressure_file_module_exists_and_refuses_unsupported_platform(tmp_path):
    m = module()
    if not shutil.rmtree.avoids_symlink_attacks:
        with pytest.raises(OSError):
            m.inspect(tmp_path, str(uuid.uuid4()), lambda: None)


@pytest.mark.skipif(
    not shutil.rmtree.avoids_symlink_attacks, reason="Linux no-follow dirfd cleanup"
)
def test_owned_files_count_and_cleanup_leave_other_scopes(tmp_path):
    m = module()
    key = str(uuid.uuid4())
    for category in ("daily", "daily-playback", "daily-exports", "daily-thumbnails", "clips"):
        (tmp_path / category).mkdir()
    (tmp_path / "daily" / key).mkdir()
    (tmp_path / "daily" / key / "a.ts").write_bytes(b"a" * 10)
    (tmp_path / "daily-exports" / (key + ".mp4")).write_bytes(b"b" * 20)
    (tmp_path / "daily-thumbnails" / (key + ".jpg")).write_bytes(b"c" * 5)
    protected = tmp_path / "clips" / "keep.mp4"
    protected.write_bytes(b"keep")
    assert m.inspect(tmp_path, key, lambda: None)["bytes"] == 35
    m.remove(tmp_path, key, lambda: None)
    assert m.inspect(tmp_path, key, lambda: None)["bytes"] == 0
    assert protected.read_bytes() == b"keep"


@pytest.mark.skipif(
    not shutil.rmtree.avoids_symlink_attacks, reason="Linux no-follow dirfd cleanup"
)
@pytest.mark.parametrize("link_scope", ["category", "day", "leaf"])
def test_symlinks_rejected_before_any_owned_files_removed(tmp_path, link_scope):
    m = module()
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    key = str(uuid.uuid4())
    category = root / "daily"
    category.mkdir()
    if link_scope == "category":
        category.rmdir()
        category.symlink_to(outside, target_is_directory=True)
    elif link_scope == "day":
        (category / key).symlink_to(outside, target_is_directory=True)
    else:
        (category / key).mkdir()
        (category / key / "a.ts").write_bytes(b"good")
        (category / key / "link").symlink_to(outside / "keep")
    with pytest.raises(OSError):
        m.remove(root, key, lambda: None)
    assert (outside / "keep").read_text() == "keep"
    if link_scope == "leaf":
        assert (category / key / "a.ts").exists()


@pytest.mark.skipif(
    not shutil.rmtree.avoids_symlink_attacks, reason="Linux no-follow dirfd cleanup"
)
def test_mount_guard_failure_prevents_removal(tmp_path):
    m = module()
    key = str(uuid.uuid4())
    (tmp_path / "daily-exports").mkdir()
    path = tmp_path / "daily-exports" / (key + ".mp4")
    path.write_bytes(b"keep")

    def bad():
        raise OSError("mount changed")

    with pytest.raises(OSError):
        m.remove(tmp_path, key, bad)
    assert path.read_bytes() == b"keep"
