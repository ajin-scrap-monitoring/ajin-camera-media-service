"""Daily recording API v2; caller authentication belongs to the internal gateway."""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import re
from datetime import date

from aiohttp import web

from camera_media_service.daily_playback import build_daily_playlist
from camera_media_service.daily_queries import RESPONSE_BYTES, DailySnapshots
from camera_media_service.http_api import body_json, query_fields
from camera_media_service.media_files import open_media_file, storage_io, stream_file
from camera_media_service.recording import ApiError
from camera_media_service.retention_assessment import assess_retention
from camera_media_service.snapshot_stream import SnapshotJSONError

STATES = {"recording", "finalizing", "closed", "failed", "deleted", "expired"}


def register_daily_routes(
    app, daily, exports, *, media_details=None, storage_status=None, policy_capacity=None
) -> None:
    key = daily.recording.cursor_key
    snapshots = DailySnapshots(
        daily.recording.root / "catalog.sqlite3", response_bytes=RESPONSE_BYTES
    )
    slot = asyncio.Semaphore(1)
    admitted = 0

    async def read_snapshot(function):
        nonlocal admitted
        if admitted >= 4:
            raise ApiError(503, "SERVICE_BUSY", "Too many catalogue queries; retry later.")
        admitted += 1
        try:
            async with slot:
                try:

                    def guarded():
                        daily.recording.storage_guard()
                        return function()

                    return await storage_io(guarded)
                except SnapshotJSONError as exc:
                    raise ApiError(
                        503,
                        "CATALOG_METADATA_ERROR",
                        "Saved catalogue metadata cannot be read within safe bounds.",
                    ) from exc
        finally:
            admitted -= 1

    async def media_info(request):
        query_fields(request, ())
        await exports.refresh_retention(daily.clock())
        return await storage_io(
            web.json_response,
            await media_details.day(request.match_info["recording_id"]),
            headers={"Cache-Control": "no-store"},
        )

    async def retention_status(request):
        query_fields(request, ())
        async with daily.lock:
            policies = {camera: daily._policy(camera) for camera in daily.recording.cameras}
            pending = [
                {"camera_id": camera, **values} for camera, values in daily.capacity_pending.items()
            ]
        samples = await read_snapshot(lambda: snapshots.retention_samples(policies))
        storage = await storage_status.snapshot()
        settings = daily.settings
        result = assess_retention(
            policies,
            samples,
            storage,
            workspace=getattr(settings, "daily_archive_workspace_bytes", 0),
            buffer=daily.recording.max_buffer_bytes,
            minimum_free=settings.min_free_storage_bytes,
            filesystem_total=storage.get("filesystem_total_bytes"),
        )
        result.update(
            observed_at=storage.get("observed_at"),
            storage=storage,
            recording_capacity_pending=pending,
        )
        if storage["status"] != "ready":
            result["status"] = "unknown"
        return await storage_io(web.json_response, result, headers={"Cache-Control": "no-store"})

    async def policy(request):
        query_fields(request, ())
        camera = request.match_info["camera_id"]
        if request.method in ("PUT", "POST"):
            result = await policy_capacity.evaluate(
                camera, await body_json(request), kind="daily", apply=request.method == "PUT"
            )
        else:
            result = await daily.get_policy(camera)
        return await storage_io(web.json_response, result, headers={"Cache-Control": "no-store"})

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
        daily.recording._camera(camera)
        summary = request.path.endswith("/summary")
        binding = {
            "kind": "daily_summary" if summary else "daily",
            "camera_id": camera,
            "filters": filters,
            "limit": limit,
        }
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
        items, has_more = await read_snapshot(
            lambda: snapshots.listing(camera, filters, limit, after, summary=summary)
        )
        cursor = None
        if has_more:
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
        return await storage_io(
            web.json_response,
            {"items": items, "next_cursor": cursor},
            headers={"Cache-Control": "no-store"},
        )

    async def detail(request):
        query_fields(request, ())
        recording_id = request.match_info["recording_id"]
        await exports.refresh_retention(daily.clock())
        if request.method == "DELETE":
            await exports.delete_day(recording_id)
            return web.Response(status=204)
        body = await read_snapshot(lambda: snapshots.read(recording_id))
        return await storage_io(web.json_response, body, headers={"Cache-Control": "no-store"})

    async def summary_detail(request):
        query_fields(request, ())
        await exports.refresh_retention(daily.clock())
        body = await read_snapshot(
            lambda: snapshots.read(request.match_info["recording_id"], summary=True)
        )
        return await storage_io(web.json_response, body, headers={"Cache-Control": "no-store"})

    async def timeline(request):
        query_fields(request, ("limit", "cursor"))
        recording_id = request.match_info["recording_id"]
        field = "gaps" if request.path.endswith("/gaps") else "available_intervals"
        try:
            limit = int(request.query.get("limit", "100"))
            if not 1 <= limit <= 500:
                raise ValueError()
        except ValueError as exc:
            raise ApiError(400, "INVALID_ARGUMENT", "limit must be between 1 and 500.") from exc
        binding = {
            "kind": "daily_timeline",
            "recording_id": recording_id,
            "field": field,
            "limit": limit,
        }
        offset, snapshot = 0, None
        if "cursor" in request.query:
            try:
                encoded = request.query["cursor"]
                if len(encoded) > 4096:
                    raise ValueError()
                raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
                signature, raw = raw[:32], raw[32:]
                if not hmac.compare_digest(signature, hmac.digest(key, raw, "sha256")):
                    raise ValueError()
                payload = json.loads(raw)
                if payload["binding"] != binding or payload["expires"] < daily.clock():
                    raise ValueError()
                offset, snapshot = payload["offset"], payload["snapshot"]
                if type(offset) is not int or offset < 0 or not isinstance(snapshot, str):
                    raise ValueError()
            except (ValueError, KeyError, TypeError) as exc:
                raise ApiError(400, "INVALID_CURSOR", "Invalid or expired cursor.") from exc
        await exports.refresh_retention(daily.clock())
        body = await read_snapshot(
            lambda: snapshots.page(recording_id, field, offset, limit, fingerprint=snapshot)
        )
        after = body.pop("next_offset")
        body["next_cursor"] = None
        if after is not None:
            raw = json.dumps(
                {
                    "binding": binding,
                    "expires": daily.clock() + 3600,
                    "offset": after,
                    "snapshot": body["snapshot"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            body["next_cursor"] = base64.urlsafe_b64encode(
                hmac.digest(key, raw, "sha256") + raw
            ).decode()
        return await storage_io(web.json_response, body, headers={"Cache-Control": "no-store"})

    async def playlist(request):
        query_fields(request, ())
        await exports.refresh_retention(daily.clock())
        day, segments = await daily.playback_snapshot(request.match_info["recording_id"])
        if not segments:
            raise ApiError(409, "PLAYBACK_NOT_READY", "No completed media segments.")
        return web.Response(
            text=await storage_io(build_daily_playlist, day, segments),
            content_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store"},
        )

    async def prepare(request):
        query_fields(request, ())
        if request.can_read_body and await body_json(request):
            raise ApiError(400, "INVALID_ARGUMENT", "Export request takes an empty object.")
        summary = request.path.endswith("/exports/summary")
        if summary:
            body, _ = await exports.request_export(request.match_info["recording_id"], summary=True)
        else:
            body, _ = await exports.request_export(request.match_info["recording_id"])
            body = await read_snapshot(lambda: snapshots.read(body["recording_id"]))
        location = f"/api/v2/daily-recordings/{body['recording_id']}"
        headers = {"Location": location + ("/summary" if summary else "")}
        if summary:
            headers["Cache-Control"] = "no-store"
        return await storage_io(
            web.json_response,
            body,
            status=200 if body["export_state"] == "ready" else 202,
            headers=headers,
        )

    async def segment(request):
        query_fields(request, ())
        await exports.refresh_retention(daily.clock())
        recording_id = request.match_info["recording_id"]
        segment_id = request.match_info["segment_id"]
        async with daily.lock:
            day = await storage_io(daily.catalog.get_state, recording_id)
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
                try:
                    file, size = await open_media_file(
                        path,
                        root=daily.recording.root / "daily",
                        guard=getattr(daily.recording, "storage_guard", None),
                    )
                except OSError as exc:
                    raise ApiError(
                        503, "RECORDING_STORAGE_ERROR", "Saved media unavailable."
                    ) from exc
        if archived:
            file, size = await exports.playback.open_segment(recording_id, segment_id)
        return await stream_file(
            request,
            file,
            size=size,
            filename=segment_id + ".ts",
            content_type="video/mp2t",
            guard=getattr(daily.recording, "storage_guard", None),
        )

    async def content(request):
        query_fields(request, ("download",))
        if request.query.get("download", "false") not in ("true", "false"):
            raise ApiError(400, "INVALID_ARGUMENT", "download must be true or false.")
        await exports.refresh_retention(daily.clock())
        recording_id = request.match_info["recording_id"]
        async with daily.lock:
            day = await storage_io(daily.catalog.get_state, recording_id)
            exports._accessible(day)
            if day["export_state"] != "ready":
                raise ApiError(409, "EXPORT_NOT_READY", "Prepare the daily MP4 first.")
            path = exports.output / (day["recording_id"] + ".mp4")
            try:
                file, size = await open_media_file(
                    path,
                    root=exports.output,
                    guard=getattr(daily.recording, "storage_guard", None),
                )
            except OSError as exc:
                raise ApiError(503, "RECORDING_STORAGE_ERROR", "Prepared MP4 unavailable.") from exc
        return await stream_file(
            request,
            file,
            size=size,
            filename=f"daily_{day['recording_date']}_{day['recording_id']}.mp4",
            content_type="video/mp4",
            guard=getattr(daily.recording, "storage_guard", None),
        )

    prefix = "/api/v2"
    if storage_status is not None:
        app.router.add_get(
            prefix + "/recording-retention-status", retention_status, allow_head=False
        )
    app.router.add_get(
        prefix + "/cameras/{camera_id}/daily-recording-policy", policy, allow_head=False
    )
    app.router.add_put(prefix + "/cameras/{camera_id}/daily-recording-policy", policy)
    app.router.add_post(prefix + "/cameras/{camera_id}/daily-recording-policy/validate", policy)
    app.router.add_get(prefix + "/cameras/{camera_id}/daily-recordings", listing, allow_head=False)
    app.router.add_get(
        prefix + "/cameras/{camera_id}/daily-recordings/summary", listing, allow_head=False
    )
    base = prefix + "/daily-recordings/{recording_id}"
    app.router.add_get(base, detail, allow_head=False)
    app.router.add_get(base + "/summary", summary_detail, allow_head=False)
    app.router.add_get(base + "/gaps", timeline, allow_head=False)
    app.router.add_get(base + "/available-intervals", timeline, allow_head=False)
    app.router.add_delete(base, detail)
    app.router.add_get(base + "/hls/index.m3u8", playlist)
    app.router.add_get(base + "/media/{segment_id}.ts", segment)
    app.router.add_post(base + "/exports", prepare)
    app.router.add_post(base + "/exports/summary", prepare)
    app.router.add_get(base + "/content", content)
    app.router.add_get(base + "/media-info", media_info, allow_head=False)
