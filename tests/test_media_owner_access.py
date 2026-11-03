import time

import pytest

from camera_media_service.media_details import MediaDetails
from camera_media_service.pipeline import LiveFrameHub
from camera_media_service.recording import ApiError, RecordingService, iso
from camera_media_service.store import LatestFrameStore


@pytest.mark.asyncio
async def test_persisted_clip_info_still_rejects_unregistered_owner(tmp_path):
    service = RecordingService(tmp_path, {"camera-main"}, LiveFrameHub(LatestFrameStore()))
    try:
        body, _ = await service.create_clip("camera-main", "owner-test")
        body.update(status="ready", camera_id="removed-camera", expires_at=iso(time.time() + 60))
        with service.db:
            service._save(body)
        details = MediaDetails(service)
        with pytest.raises(ApiError) as error:
            await details.clip(body["clip_id"])
        assert error.value.status == 404
        assert error.value.code == "CAMERA_NOT_FOUND"
    finally:
        await service.close()
