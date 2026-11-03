"""Stream an already-open media file with bounded reads and single Range support."""

from __future__ import annotations

import asyncio
import os
import re

from aiohttp import web


async def _settle_worker(task):
    # A second cancellation must not cancel the worker's asyncio wrapper while
    # its OS operation still owns a handle, lease, or catalogue lock.
    completion = asyncio.gather(task, return_exceptions=True)
    while True:
        try:
            return (await asyncio.shield(completion))[0]
        except asyncio.CancelledError:
            continue


async def storage_io(function, /, *args, **kwargs):
    """Keep a lease/lock until the file worker finishes, including cancellation."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _settle_worker(task)
        raise


async def open_media_file(path, *, root=None, guard=None, mode="rb"):
    """Open and inspect the same handle off-loop while the caller retains its lock."""

    def opened():
        if guard is not None:
            guard()
        if root is not None and not path.resolve().is_relative_to(root.resolve()):
            raise OSError("Invalid media path")
        file = path.open(mode)
        try:
            return file, os.fstat(file.fileno()).st_size
        except BaseException:
            file.close()
            raise

    task = asyncio.create_task(asyncio.to_thread(opened))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        result = await _settle_worker(task)
        if isinstance(result, tuple):
            await storage_io(result[0].close)
        raise


async def stream_file(
    request,
    file,
    *,
    size: int,
    filename: str,
    content_type: str,
    cache_control: str = "no-store",
    guard=None,
) -> web.StreamResponse:
    try:
        start, end, status = 0, size - 1, 200
        raw = request.headers.get("Range", "")
        if raw and "," not in raw:
            try:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", raw)
                if match is None or not any(match.groups()):
                    raise ValueError()
                left, right = match.groups()
                if left:
                    start = int(left)
                    end = min(int(right), size - 1) if right else size - 1
                else:
                    length = int(right)
                    if length <= 0:
                        raise ValueError()
                    start = max(0, size - length)
                if start >= size or end < start:
                    raise ValueError()
                status = 206
            except ValueError:
                return web.json_response(
                    {"error": {"code": "RANGE_NOT_SATISFIABLE", "message": "Invalid range."}},
                    status=416,
                    headers={"Content-Range": f"bytes */{size}"},
                )
        disposition = "attachment" if request.query.get("download") == "true" else "inline"
        headers = {
            "Content-Type": content_type,
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Cache-Control": cache_control,
            "Content-Disposition": f'{disposition}; filename="{filename}"',
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        response = web.StreamResponse(status=status, headers=headers)
        await response.prepare(request)
        if request.method == "HEAD":
            return response
        await storage_io(file.seek, start)
        remaining = end - start + 1

        def read_chunk(length):
            if guard is not None:
                guard()
            return file.read(length)

        try:
            while remaining:
                data = await storage_io(read_chunk, min(65536, remaining))
                if not data:
                    break
                await asyncio.wait_for(response.write(data), 30)
                remaining -= len(data)
        except (OSError, TimeoutError):
            if request.transport:
                request.transport.close()
        return response
    finally:
        await storage_io(file.close)
