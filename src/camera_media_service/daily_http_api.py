"""Daily recording API v2; caller authentication belongs to the internal gateway."""

from __future__ import annotations

import base64
import hmac
import json
import re
from datetime import date

from aiohttp import web

from camera_media_service.daily_playback import build_daily_playlist
from camera_media_service.http_api import body_json, query_fields
from camera_media_service.media_files import stream_file
from camera_media_service.recording import ApiError

STATES = {"recording", "finalizing", "closed", "failed", "deleted", "expired"}


def register_daily_routes(app, daily, exports, *, media_details=None) -> None:
    key = daily.recording.cursor_key

    async def media_info(request):
        query_fields(request, ())
        await exports.refresh_retention(daily.clock())
        return web.json_response(
            await media_details.day(request.match_info["recording_id"]),
            headers={"Cache-Control": "no-store"},
        )

    async def policy(request):
        query_fields(request, ())
        camera = request.match_info["camera_id"]
        result = (
            await daily.set_policy(camera, await body_json(request))
            if request.method == "PUT"
            else await daily.get_policy(camera)
        )
        return web.json_response(result)

    async def listing(request):
        query_fields(request, ("from_date", "to_date", "status", "limit", "cursor"))
        filters = {k: v for k, v in request.query.items() if k not in ("limit", "cursor")}
        try:
            limit = int(request.query.get("limit", "20"))
            if not 1 <= limit <= 100:
                raise ValueError()
            for name in ("from_date", "to_date"):
                if name in filters:
                    if not re.fullmatch(r"\d{4}-\d\d-\d\d", filters[name]):
                        raise ValueError()
                    date.fromisoformat(filters[name])
            if (
                "from_date" in filters
                and "to_date" in filters
                and filters["from_date"] >= filters["to_date"]
            ):
                raise ValueError()
            if "status" in filters and filters["status"] not in STATES:
                raise ValueError()
        except ValueError as exc:
            raise ApiError(400, "INVALID_ARGUMENT", "Invalid daily filter.") from exc
        camera = request.match_info["camera_id"]
        binding = {"kind": "daily", "camera_id": camera, "filters": filters, "limit": limit}
        after = None
        if "cursor" in request.query:
            try:
                raw = base64.b64decode(request.query["cursor"], altchars=b"-_", validate=True)
                signature, raw = raw[:32], raw[32:]
                if not hmac.compare_digest(signature, hmac.digest(key, raw, "sha256")):
                    raise ValueError()
                payload = json.loads(raw)
                if payload["binding"] != binding or payload["expires"] < daily.clock():
                    raise ValueError()
                after = payload["after"]
                if not isinstance(after, list) or len(after) != 1:
                    raise ValueError()
                date.fromisoformat(after[0])
            except (ValueError, KeyError, TypeError) as exc:
                raise ApiError(400, "INVALID_CURSOR", "Invalid cursor.") from exc
        await exports.refresh_retention(daily.clock())
        items = await daily.list_days(camera, filters, limit + 1, after)
        cursor = None
        if len(items) > limit:
            items = items[:limit]
            raw = json.dumps(
                {
                    "binding": binding,
                    "expires": daily.clock() + 86400,
                    "after": [items[-1]["recording_date"]],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            cursor = base64.urlsafe_b64encode(hmac.digest(key, raw, "sha256") + raw).decode()
        return web.json_response({"items": items, "next_cursor": cursor})

    async def detail(request):
        query_fields(request, ())
        recording_id = request.match_info["recording_id"]
        await exports.refresh_retention(daily.clock())
        if request.method == "DELETE":
            await exports.delete_day(recording_id)
            return web.Response(status=204)
        return web.json_response(await daily.get_day(recording_id))

    async def playlist(request):
        query_fields(request, ())
        await exports.refresh_retention(daily.clock())
        day, segments = await daily.playback_snapshot(request.match_info["recording_id"])
        if not segments:
            raise ApiError(409, "PLAYBACK_NOT_READY", "No completed media segments.")
        return web.Response(
            text=build_daily_playlist(day, segments),
            content_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store"},
        )

    async def prepare(request):
        query_fields(request, ())
        if request.can_read_body and await body_json(request):
            raise ApiError(400, "INVALID_ARGUMENT", "Export request takes an empty object.")
        body, _ = await exports.request_export(request.match_info["recording_id"])
        return web.json_response(
            body,
            status=200 if body["export_state"] == "ready" else 202,
            headers={"Location": f"/api/v2/daily-recordings/{body['recording_id']}"},
        )

    async def segment(request):
        query_fields(request, ())
        await exports.refresh_retention(daily.clock())
        recording_id = request.match_info["recording_id"]
        segment_id = request.match_info["segment_id"]
        async with daily.lock:
            day = daily.catalog.get_day(recording_id)
            exports._accessible(day)
            row = daily.db.execute(
                "SELECT body FROM daily_segments WHERE id=? AND day_id=? AND status='ready'",
                (segment_id, recording_id),
            ).fetchone()
            if row is None:
                raise ApiError(404, "SEGMENT_NOT_FOUND", "Completed segment does not exist.")
            item = json.loads(row[0])
            archived = day.get("archive_state") == "mp4"
            if not archived:
                path = daily.segment_path(item)
                if not path.resolve().is_relative_to((daily.recording.root / "daily").resolve()):
                    raise OSError("Invalid media path")
                try:
                    file = path.open("rb")
                except OSError as exc:
                    raise ApiError(
                        503, "RECORDING_STORAGE_ERROR", "Saved media unavailable."
                    ) from exc
                file.seek(0, 2)
                size = file.tell()
        if archived:
            file, size = await exports.playback.open_segment(recording_id, segment_id)
        return await stream_file(
            request, file, size=size, filename=segment_id + ".ts", content_type="video/mp2t"
        )

    async def content(request):
        query_fields(request, ("download",))
        if request.query.get("download", "false") not in ("true", "false"):
            raise ApiError(400, "INVALID_ARGUMENT", "download must be true or false.")
        await exports.refresh_retention(daily.clock())
        recording_id = request.match_info["recording_id"]
        async with daily.lock:
            day = daily.catalog.get_day(recording_id)
            exports._accessible(day)
            if day["export_state"] != "ready":
                raise ApiError(409, "EXPORT_NOT_READY", "Prepare the daily MP4 first.")
            path = exports.output / (day["recording_id"] + ".mp4")
            if not path.resolve().is_relative_to(exports.output.resolve()):
                raise OSError("Invalid media path")
            try:
                file = path.open("rb")
            except OSError as exc:
                raise ApiError(503, "RECORDING_STORAGE_ERROR", "Prepared MP4 unavailable.") from exc
            file.seek(0, 2)
            size = file.tell()
        return await stream_file(
            request,
            file,
            size=size,
            filename=f"daily_{day['recording_date']}_{day['recording_id']}.mp4",
            content_type="video/mp4",
        )

    prefix = "/api/v2"
    app.router.add_get(
        prefix + "/cameras/{camera_id}/daily-recording-policy", policy, allow_head=False
    )
    app.router.add_put(prefix + "/cameras/{camera_id}/daily-recording-policy", policy)
    app.router.add_get(prefix + "/cameras/{camera_id}/daily-recordings", listing, allow_head=False)
    base = prefix + "/daily-recordings/{recording_id}"
    app.router.add_get(base, detail, allow_head=False)
    app.router.add_delete(base, detail)
    app.router.add_get(base + "/hls/index.m3u8", playlist)
    app.router.add_get(base + "/media/{segment_id}.ts", segment)
    app.router.add_post(base + "/exports", prepare)
    app.router.add_get(base + "/content", content)
    app.router.add_get(base + "/media-info", media_info, allow_head=False)
