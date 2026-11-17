import asyncio
import os
import shutil
import uuid

import pytest
from test_capacity_cleanup import Fixture, cleaner, measurement

from camera_media_service.recording import ApiError


@pytest.mark.asyncio
async def test_rejected_uncommitted_intent_does_not_delete_after_recovery(tmp_path):
    f = Fixture(tmp_path)
    real = f.delete_day

    async def blocked(*args, **kwargs):
        raise ApiError(409, "EXPORT_IN_PROGRESS", "pin")

    f.delete_day = blocked
    c = cleaner(f)
    await c.tick(100)
    f.budget = measurement(800)
    f.delete_day = real
    c = cleaner(f)
    await c.tick(110)
    assert not f.deleted and c.status()["pending"] is None and not c.status()["active"]


@pytest.mark.asyncio
async def test_obsolete_intent_is_reselected_instead_of_blocking_forever(tmp_path):
    f = Fixture(tmp_path)
    real = f.delete_day

    async def blocked(*args, **kwargs):
        raise ApiError(409, "EXPORT_IN_PROGRESS", "pin")

    f.delete_day = blocked
    c = cleaner(f)
    await c.tick(100)
    f.days.pop(0)

    async def state(key):
        return "obsolete" if key == "oldest" else "uncommitted"

    f.pressure_intent_state = state
    f.delete_day = real
    await c.tick(110)
    assert f.deleted == ["newer"]


@pytest.mark.asyncio
async def test_cancelled_selection_abandoned_below_stop_but_committed_cleanup_resumes(tmp_path):
    f = Fixture(tmp_path)
    entered = asyncio.Event()

    async def pause(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    real = f.delete_day
    f.delete_day = pause
    c = cleaner(f)
    task = asyncio.create_task(c.tick(100))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    f.budget = measurement(800)
    f.delete_day = real
    c = cleaner(f)
    await c.tick(110)
    assert not f.deleted
    # A durable tombstone still must finish its file cleanup after pressure recovers.
    f.budget = measurement(910)
    f.delete_day = pause
    entered.clear()
    task = asyncio.create_task(c.tick(120))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async def committed(key):
        return "committed"

    f.pressure_intent_state = committed
    f.budget = measurement(800)
    f.delete_day = real
    c = cleaner(f)
    await c.tick(130)
    assert f.deleted == ["oldest"]


@pytest.mark.skipif(not shutil.rmtree.avoids_symlink_attacks, reason="Linux mount IDs")
@pytest.mark.parametrize("scope", ["category", "directory", "file"])
def test_same_device_mount_boundaries_are_rejected(tmp_path, monkeypatch, scope):
    from camera_media_service import pressure_files as m

    assert hasattr(m, "_mount_id"), "same-device mount detection missing"
    key = str(uuid.uuid4())
    category = tmp_path / "daily"
    category.mkdir()
    day = category / key
    day.mkdir()
    leaf = day / "bound.bin"
    leaf.write_bytes(b"keep")
    target = category if scope == "category" else day if scope == "directory" else leaf
    original = m._mount_id

    def boundary(fd):
        value = original(fd)
        return value + 1 if os.readlink("/proc/self/fd/" + str(fd)) == str(target) else value

    monkeypatch.setattr(m, "_mount_id", boundary)
    with pytest.raises(OSError):
        m.remove(tmp_path, key, lambda: None)
    assert leaf.read_bytes() == b"keep"
