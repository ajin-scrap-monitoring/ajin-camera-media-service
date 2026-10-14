"""Local backend JSON API, multipart JPEG stream, and clip files."""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import re
import sqlite3
import time

from aiohttp import web

from camera_media_service.media_files import stream_file
from camera_media_service.recording import ACTIVE, ApiError, RecordingService, epoch

LOG = logging.getLogger(__name__)
STATES = {*ACTIVE, "ready", "failed", "deleted", "expired"}


def error_response(error: ApiError, headers=None):
    headers = dict(headers or {})
    return web.json_response(
        {"error": {"code": error.code, "message": error.message}},
        status=error.status,
        headers=headers,
    )


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


async def body_json(request):
    if request.content_type != "application/json":
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "Use application/json.")
    try:
        value = json.loads(
            await request.text(),
            object_pairs_hook=strict_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, UnicodeError) as exc:
        raise ApiError(400, "INVALID_ARGUMENT", "Invalid JSON body.") from exc
    if not isinstance(value, dict):
        raise ApiError(400, "INVALID_ARGUMENT", "JSON object required.")
    return value


def query_fields(request, allowed):
    if set(request.query) - set(allowed) or any(
        len(request.query.getall(k)) != 1 for k in request.query
    ):
        raise ApiError(400, "INVALID_ARGUMENT", "Unknown or duplicate query parameter.")


def create_http_app(service: RecordingService, *, daily=None, exports=None) -> web.Application:
    key = service.cursor_key
    viewers = 0

    @web.middleware
    async def boundary(request, handler):
        try:
            return await handler(request)
        except ApiError as exc:
            return error_response(exc)
        except (OSError, sqlite3.Error):
            LOG.exception("Media storage operation failed")
            return error_response(ApiError(503, "STORAGE_UNAVAILABLE", "Storage unavailable."))
        except web.HTTPException as exc:
            return error_response(ApiError(exc.status, "INVALID_ARGUMENT", exc.reason))
        except Exception:
            LOG.exception("Media API request failed")
            return error_response(ApiError(500, "INTERNAL_ERROR", "Internal service error."))

    app = web.Application(middlewares=[boundary], client_max_size=16384)

    async def lifecycle(app):
        try:
            await service.start()
            if daily is not None:
                await daily.start()
                await exports.start()
            yield
        finally:
            if daily is not None:
                await exports.close()
                await daily.close()
            await service.close()

    app.cleanup_ctx.append(lifecycle)

    async def policy(request):
        query_fields(request, ())
        camera = request.match_info["camera_id"]
        if request.method == "PUT":
            result = await service.set_policy(camera, await body_json(request))
        else:
            result = await service.get_policy(camera)
        return web.json_response(result)

    async def create(request):
        query_fields(request, ())
        data = await body_json(request)
        if set(data) != {"camera_id", "event_id"}:
            raise ApiError(400, "INVALID_ARGUMENT", "camera_id and event_id are required.")
        result, created = await service.create_clip(data["camera_id"], data["event_id"])
        fields = (
            "clip_id",
            "event_id",
            "camera_id",
            "status",
            "accepted_at",
            "requested_start_at",
            "requested_end_at",
        )
        return web.json_response(
            {k: result[k] for k in fields},
            status=202 if created else 200,
            headers={"Location": f"/api/v1/clips/{result['clip_id']}"},
        )

    async def detail(request):
        query_fields(request, ())
        clip_id = request.match_info["clip_id"]
        if request.method == "DELETE":
            await service.delete_clip(clip_id)
            return web.Response(status=204)
        return web.json_response(await service.get_clip(clip_id))

    def encode_cursor(payload):
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(hmac.digest(key, raw, "sha256") + raw).decode()

    def decode_cursor(value, binding):
        try:
            raw = base64.b64decode(value, altchars=b"-_", validate=True)
            signature, raw = raw[:32], raw[32:]
            if not hmac.compare_digest(signature, hmac.digest(key, raw, "sha256")):
                raise ValueError()
            payload = json.loads(raw)
            if payload["expires"] < time.time() or payload["binding"] != binding:
                raise ValueError()
            return payload["after"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ApiError(400, "INVALID_CURSOR", "Invalid or expired cursor.") from exc

    async def listing(request):
        query_fields(request, ("camera_id", "event_id", "from", "to", "status", "limit", "cursor"))
        filters = {k: v for k, v in request.query.items() if k not in ("limit", "cursor")}
        try:
            limit = int(request.query.get("limit", "20"))
            if not 1 <= limit <= 100:
                raise ValueError()
            if "status" in filters and filters["status"] not in STATES:
                raise ValueError()
            for name in ("from", "to"):
                if name in filters:
                    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", filters[name]):
                        raise ValueError()
                    filters[name] = epoch(filters[name])
            if "from" in filters and "to" in filters and filters["from"] >= filters["to"]:
                raise ValueError()
        except (ValueError, OverflowError) as exc:
            raise ApiError(400, "INVALID_ARGUMENT", "Invalid list filter.") from exc
        binding = {"filters": filters, "limit": limit}
        after = (
            decode_cursor(request.query["cursor"], binding) if "cursor" in request.query else None
        )
        items = await service.list_clips(filters, limit + 1, after)
        cursor = None
        if len(items) > limit:
            items = items[:limit]
            cursor = encode_cursor(
                {
                    "binding": binding,
                    "expires": time.time() + 86400,
                    "after": [epoch(items[-1]["accepted_at"]), items[-1]["clip_id"]],
                }
            )
        return web.json_response({"items": items, "next_cursor": cursor})

    async def live(request):
        nonlocal viewers
        query_fields(request, ())
        camera = request.match_info["camera_id"]
        service._camera(camera)
        frame = service.hub.latest.get(camera)
        if frame is None or time.time() - frame.received_at >= 5:
            raise ApiError(503, "CAMERA_UNAVAILABLE", "No recent frame.")
        if viewers >= 100:
            raise ApiError(503, "SERVICE_BUSY", "Too many live sessions.")
        viewers += 1
        queue = service.hub.subscribe(maxsize=1)
        response = web.StreamResponse(
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=frame",
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            }
        )
        try:
            await response.prepare(request)
            while True:
                if frame.camera_id == camera:
                    data = (
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(frame.jpeg)).encode()
                        + b"\r\n\r\n"
                        + frame.jpeg
                        + b"\r\n"
                    )
                    await asyncio.wait_for(response.write(data), 5)
                latest = service.hub.latest.get(camera)
                remaining = 5 - (time.time() - latest.received_at)
                if remaining <= 0:
                    break
                frame = await asyncio.wait_for(queue.get(), remaining)
                queue.task_done()
        except (TimeoutError, ConnectionError):
            pass
        finally:
            viewers -= 1
            service.hub.unsubscribe(queue)
        return response

    async def content(request):
        query_fields(request, ("download",))
        if request.query.get("download", "false") not in ("true", "false"):
            raise ApiError(400, "INVALID_ARGUMENT", "download must be true or false.")
        clip_id = request.match_info["clip_id"]
        async with service.lock:
            body = service._get(clip_id)
            await service._expire(body)
            status = body["status"]
            if status in ("deleted", "expired"):
                raise ApiError(410, "CLIP_" + status.upper(), "Clip is no longer available.")
            if status != "ready":
                raise ApiError(409, "CLIP_NOT_READY", "Clip is not ready.")
            # Open under the catalog lock: a concurrent deletion cannot win between check/open.
            file = (service.clips / f"{body['clip_id']}.mp4").open("rb")
        try:
            file.seek(0, 2)
            size = file.tell()
            return await stream_file(
                request,
                file,
                size=size,
                filename=body["clip_id"] + ".mp4",
                content_type="video/mp4",
            )
        finally:
            file.close()

    prefix = "/api/v1"
    app.router.add_get(prefix + "/cameras/{camera_id}/recording-policy", policy, allow_head=False)
    app.router.add_put(prefix + "/cameras/{camera_id}/recording-policy", policy)
    app.router.add_get(prefix + "/cameras/{camera_id}/live", live, allow_head=False)
    app.router.add_post(prefix + "/clips", create)
    app.router.add_get(prefix + "/clips", listing, allow_head=False)
    app.router.add_get(prefix + "/clips/{clip_id}", detail, allow_head=False)
    app.router.add_delete(prefix + "/clips/{clip_id}", detail)
    app.router.add_get(prefix + "/clips/{clip_id}/content", content, allow_head=False)
    if daily is not None:
        from camera_media_service.daily_http_api import register_daily_routes

        register_daily_routes(app, daily, exports)
    return app
