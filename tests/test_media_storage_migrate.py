import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest


def load_tool():
    spec = importlib.util.spec_from_file_location(
        "media_storage_migrate", Path(__file__).parents[1] / "tools/media_storage_migrate.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def trees(tmp_path):
    source, target = tmp_path / "ssd", tmp_path / "sd"
    source.mkdir()
    target.mkdir()
    (source / "clips").mkdir()
    (source / "clips" / "existing.mp4").write_bytes(b"original footage")
    with sqlite3.connect(source / ".catalog.sqlite3") as db:
        db.execute("CREATE TABLE clips (id TEXT PRIMARY KEY, path TEXT)")
        db.execute("INSERT INTO clips VALUES ('preserved-id', 'clips/existing.mp4')")
    return source, target


def test_manifest_includes_hidden_catalog_and_content_hash(trees):
    tool = load_tool()
    source, _ = trees
    manifest = tool.build_manifest(source)
    assert {f["path"] for f in manifest["files"]} == {".catalog.sqlite3", "clips/existing.mp4"}
    assert manifest["sqlite"][".catalog.sqlite3"]["tables"] == {"clips": 1}
    before = manifest["files"]
    (source / "clips/existing.mp4").write_bytes(b"replaced footage")
    assert before != tool.build_manifest(source)["files"]


def test_copy_verify_preserves_source_ids_permissions_and_is_repeatable(trees):
    tool = load_tool()
    source, target = trees
    calls = []

    def quiescent():
        calls.append("checked")

    manifest = tool.copy_storage(source, target, quiescent=quiescent, guard=lambda: None)
    tool.verify_storage(source, target, manifest, quiescent=quiescent, guard=lambda: None)
    tool.copy_storage(source, target, quiescent=quiescent, guard=lambda: None)
    assert calls
    assert (source / "clips/existing.mp4").read_bytes() == b"original footage"
    with sqlite3.connect(target / ".catalog.sqlite3") as db:
        assert db.execute("SELECT id FROM clips").fetchone() == ("preserved-id",)
    assert (source / "clips/existing.mp4").stat().st_mode == (
        target / "clips/existing.mp4"
    ).stat().st_mode


def test_interrupted_copy_retry_and_refusal_to_overwrite_new_data(trees, monkeypatch):
    tool = load_tool()
    source, target = trees
    original = tool._copy_file
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("interrupted")
        return original(*args, **kwargs)

    monkeypatch.setattr(tool, "_copy_file", interrupted)
    with pytest.raises(OSError):
        tool.copy_storage(source, target, quiescent=lambda: None, guard=lambda: None)
    monkeypatch.setattr(tool, "_copy_file", original)
    tool.copy_storage(source, target, quiescent=lambda: None, guard=lambda: None)
    (target / "clips/existing.mp4").write_bytes(b"new recording")
    with pytest.raises(tool.MigrationError, match="mismatch"):
        tool.copy_storage(source, target, quiescent=lambda: None, guard=lambda: None)
    assert (target / "clips/existing.mp4").read_bytes() == b"new recording"


def test_copy_refuses_active_source_and_wrong_target_before_write(trees):
    tool = load_tool()
    source, target = trees

    def refuse():
        raise tool.MigrationError("active source")

    with pytest.raises(tool.MigrationError):
        tool.copy_storage(source, target, quiescent=refuse, guard=lambda: None)
    assert list(target.iterdir()) == []
    with pytest.raises(tool.MigrationError):
        tool.copy_storage(source, target, quiescent=lambda: None, guard=refuse)
    assert list(target.iterdir()) == []


def test_verify_refuses_extra_destination_files(trees):
    tool = load_tool()
    source, target = trees
    manifest = tool.copy_storage(source, target, quiescent=lambda: None, guard=lambda: None)
    (target / "new.mp4").write_bytes(b"never overwrite")
    with pytest.raises(tool.MigrationError):
        tool.verify_storage(source, target, manifest, quiescent=lambda: None, guard=lambda: None)


def test_manifest_refuses_symlinks_when_supported(trees, tmp_path):
    tool = load_tool()
    source, _ = trees
    outside = tmp_path / "outside"
    outside.write_text("private")
    try:
        (source / "link").symlink_to(outside)
    except OSError:
        pytest.skip("Windows account cannot create symlinks")
    with pytest.raises(tool.MigrationError, match="symlink"):
        tool.build_manifest(source)


def test_copy_rejects_nested_source_and_target(trees):
    tool = load_tool()
    source, _ = trees
    target = source / "nested"
    target.mkdir()
    with pytest.raises(tool.MigrationError):
        tool.copy_storage(source, target, quiescent=lambda: None, guard=lambda: None)


def test_retry_ignores_retained_interrupted_staging_files(trees):
    tool = load_tool()
    source, target = trees
    staging = target / tool.STAGING_NAME
    staging.mkdir()
    incomplete = staging / ".media-copy-interrupted"
    incomplete.write_bytes(b"partial copy")
    tool.copy_storage(source, target, quiescent=lambda: None, guard=lambda: None)
    assert incomplete.read_bytes() == b"partial copy"


def test_overlap_detects_inode_alias_even_when_lexical_paths_differ(trees, monkeypatch):
    tool = load_tool()
    source, target = trees
    actual_stat = Path.stat

    def alias_stat(path, *args, **kwargs):
        return actual_stat(source if path == target else path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", alias_stat)
    assert tool._overlaps(source, target)


def test_manifest_sqlite_reads_wal_without_mutating_source(trees):
    tool = load_tool()
    source, _ = trees
    database = source / ".catalog.sqlite3"
    db = sqlite3.connect(database)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("INSERT INTO clips VALUES ('wal-id', 'clips/wal.mp4')")
        db.commit()
        before = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
        manifest = tool.build_manifest(source)
        assert manifest["sqlite"][".catalog.sqlite3"]["tables"] == {"clips": 2}
        assert before == {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
    finally:
        db.close()


def test_quiescence_rejects_running_media_container(trees, monkeypatch):
    tool = load_tool()
    source, target = trees
    monkeypatch.setattr(
        tool,
        "_command",
        lambda args: json.dumps(
            [{"State": {"Running": True}, "Mounts": [{"Source": str(source)}]}]
        ),
    )
    with pytest.raises(tool.MigrationError, match="stopped"):
        tool.assert_quiescent(source, target, "media")


def test_quiescence_rejects_running_container_parent_bind(trees, monkeypatch):
    tool = load_tool()
    source, target = trees

    def command(args):
        if args == ["docker", "ps", "-q"]:
            return "other-writer"
        if args[-1] == "media":
            return json.dumps([{"State": {"Running": False}, "Mounts": [{"Source": str(source)}]}])
        return json.dumps([{"Mounts": [{"Source": str(target.parent)}]}])

    monkeypatch.setattr(tool, "_command", command)
    with pytest.raises(tool.MigrationError, match="Running container"):
        tool.assert_quiescent(source, target, "media")
