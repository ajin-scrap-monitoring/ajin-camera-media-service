"""Additive thumbnail control and small JPEG responses; gateway owns user auth."""

from __future__ import annotations

from aiohttp import web

from camera_media_service.media_files import storage_io
from camera_media_service.recording import ApiError
from camera_media_service.thumbnail_media import ThumbnailFailure, jpeg_dimensions
from camera_media_service.thumbnails import ThumbnailService

THUMBNAILS = web.AppKey("thumbnails", ThumbnailService)


@web.middleware
async def thumbnail_headers(request, handler):
    response = await handler(request)
    if request.path.endswith(("/thumbnail", "/thumbnail-info")):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def register_thumbnail_routes(app, service):
    from camera_media_service.http_api import body_json, query_fields

    app[THUMBNAILS] = service
    for kind, prefix, identifier in (
        ("clip", "/api/v1/clips", "clip_id"),
        ("day", "/api/v2/daily-recordings", "recording_id"),
    ):

        async def info(request, kind=kind, identifier=identifier):
            query_fields(request, ())
            return web.json_response(await service.info(kind, request.match_info[identifier]))

        async def prepare(request, kind=kind, identifier=identifier):
            query_fields(request, ())
            data = await body_json(request)
            if set(data) - {"retry_failed"} or type(data.get("retry_failed", False)) is not bool:
                raise ApiError(400, "INVALID_ARGUMENT", "Only a boolean retry_failed is accepted.")
            key = request.match_info[identifier]
            body, created = await service.prepare(
                kind, key, retry_failed=data.get("retry_failed", False)
            )
            base = "/api/v1/clips" if kind == "clip" else "/api/v2/daily-recordings"
            return web.json_response(
                body,
                status=202 if created else 200,
                headers={"Location": base + "/" + key + "/thumbnail-info"},
            )

        async def image(request, kind=kind, identifier=identifier):
            query_fields(request, ())
            file, size = await service.open_image(kind, request.match_info[identifier])
            try:
                service.recording.storage_guard()
                data = await storage_io(file.read, service.max_bytes + 1)
                if len(data) != size or len(data) > service.max_bytes:
                    raise ApiError(
                        503, "THUMBNAIL_STORAGE_UNAVAILABLE", "Thumbnail image unavailable."
                    )
                try:
                    jpeg_dimensions(data)
                except ThumbnailFailure as exc:
                    raise ApiError(
                        503, "THUMBNAIL_STORAGE_UNAVAILABLE", "Thumbnail image unavailable."
                    ) from exc
            finally:
                await storage_io(file.close)
            return web.Response(body=data, content_type="image/jpeg")

        app.router.add_get(prefix + "/{" + identifier + "}/thumbnail-info", info, allow_head=False)
        app.router.add_post(prefix + "/{" + identifier + "}/thumbnail", prepare)
        app.router.add_get(prefix + "/{" + identifier + "}/thumbnail", image)
