import io

import imageio_ffmpeg
import pytest
from PIL import Image

from camera_media_service import encoding
from camera_media_service.daily_models import StoredFrame


@pytest.mark.asyncio
async def test_daily_ts_decodes_and_preserves_real_duration_without_long_freeze(tmp_path):
    assert hasattr(encoding, "encode_daily_segment"), "Daily segment encoder missing"
    buffer = tmp_path / "buffer"
    buffer.mkdir()
    frames = []
    for i in range(12):
        raw = io.BytesIO()
        Image.new("RGB", (320, 240), (i * 15, 20, 20)).save(raw, format="JPEG")
        name = f"frame-{i}.jpg"
        (buffer / name).write_bytes(raw.getvalue())
        frames.append(StoredFrame(name, 1000 + i / 10, len(raw.getvalue())))
    path = tmp_path / "segment.ts"
    metadata = await encoding.encode_daily_segment(
        imageio_ffmpeg.get_ffmpeg_exe(),
        buffer,
        path,
        frames,
        {"width": 320, "height": 240, "fps": 30},
    )
    assert path.exists() and metadata["size_bytes"] == path.stat().st_size
    assert 1.10 <= metadata["duration"] <= 1.30
    reader = imageio_ffmpeg.read_frames(str(path))
    header = next(reader)
    assert header["size"] == (320, 240)
    assert sum(1 for _ in reader) >= 10
    assert not list(buffer.glob("*.ffconcat"))
