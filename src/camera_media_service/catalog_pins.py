"""Drained catalogue pin ownership, including cancellation during acquisition."""

import asyncio
from contextlib import asynccontextmanager

from camera_media_service.media_files import storage_io


async def finish_cleanup(operation):
    task = asyncio.create_task(operation)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


async def pin_day_locked(recording, owner, key):
    async with recording.transaction():
        await storage_io(
            recording.db.execute, "INSERT INTO clip_recording_pins VALUES(?,?)", (owner, key)
        )


async def release_day_locked(recording, owner):
    async with recording.transaction():
        await storage_io(
            recording.db.execute, "DELETE FROM clip_recording_pins WHERE clip_id=?", (owner,)
        )


async def release_day(recording, owner):
    async with recording.lock:
        await release_day_locked(recording, owner)


@asynccontextmanager
async def pinned_snapshot(recording, owner, key, snapshot):
    attempted = False
    try:
        async with recording.lock:
            result = await storage_io(snapshot)
            attempted = True
            await pin_day_locked(recording, owner, key)
        yield result
    finally:
        if attempted:
            await finish_cleanup(release_day(recording, owner))
