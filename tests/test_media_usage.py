import os
from pathlib import Path

import pytest

from camera_media_service.media_usage import iter_regular_files


def test_usage_walk_counts_all_physical_files_and_can_exclude_only_buffer(tmp_path):
    (tmp_path / "daily" / "nested").mkdir(parents=True)
    (tmp_path / "buffer").mkdir()
    for name, size in (
        ("catalog.sqlite3", 3),
        ("daily/one.ts", 5),
        ("daily/nested/two.ts", 7),
        ("buffer/frame.jpg", 11),
    ):
        (tmp_path / name).write_bytes(b"x" * size)
    files = list(iter_regular_files(tmp_path))
    assert sum(info.st_size for _, info, _ in files) == 26
    assert {category for _, _, category in files} == {None, "daily", "buffer"}
    assert (
        sum(info.st_size for _, info, _ in iter_regular_files(tmp_path, exclude={"buffer"})) == 15
    )


def test_usage_walk_counts_file_symlinks_but_does_not_recurse_directory_symlinks(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    (root / "known").write_bytes(b"abc")
    external = tmp_path / "outside"
    external.mkdir()
    (external / "large").write_bytes(b"x" * 100)
    try:
        os.symlink(root / "known", root / "alias")
        os.symlink(external, root / "directory", target_is_directory=True)
        os.symlink(root / "missing", root / "broken")
    except OSError:
        pytest.skip("Account cannot create symlinks")
    files = list(iter_regular_files(root))
    assert sum(info.st_size for _, info, _ in files) == 6
    assert {Path(path).name for path, _, _ in files} == {"known", "alias"}
