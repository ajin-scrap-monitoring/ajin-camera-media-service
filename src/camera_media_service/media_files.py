"""Stream an already-open media file with bounded reads and single Range support."""

from __future__ import annotations

import asyncio
import re

from aiohttp import web


async def stream_file(
    request, file, *, size: int, filename: str, content_type: str, cache_control: str = "no-store"
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
        file.seek(start)
        remaining = end - start + 1
        try:
            while remaining:
                data = await asyncio.to_thread(file.read, min(65536, remaining))
                if not data:
                    break
                await asyncio.wait_for(response.write(data), 30)
                remaining -= len(data)
        except (OSError, TimeoutError):
            if request.transport:
                request.transport.close()
        return response
    finally:
        file.close()
