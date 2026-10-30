import io
import struct

import imageio_ffmpeg
import pytest
from PIL import Image
from test_archive_media import prepared

from camera_media_service.clip_media import remux_clip
from camera_media_service.encoding import encode_clip, verify_archive
from camera_media_service.media_info import inspect_media_info


def top_level_atoms(path):
    """Walk ISO-BMFF atom boundaries instead of matching bytes inside H264 payloads."""
    atoms = []
    file_size = path.stat().st_size
    with path.open("rb") as file:
        while file.tell() < file_size:
            offset = file.tell()
            header = file.read(8)
            assert len(header) == 8, "Incomplete MP4 atom header"
            size, kind = struct.unpack(">I4s", header)
            header_size = 8
            if size == 1:
                extended = file.read(8)
                assert len(extended) == 8, "Incomplete extended MP4 atom header"
                size = struct.unpack(">Q", extended)[0]
                header_size = 16
            elif size == 0:
                size = file_size - offset
            assert header_size <= size <= file_size - offset, "Invalid MP4 atom boundary"
            atoms.append(kind)
            file.seek(offset + size)
    return atoms


@pytest.mark.asyncio
async def test_canonical_archive_keeps_index_after_payload_and_remains_locally_readable(tmp_path):
    segments, path = await prepared(tmp_path)
    atoms = top_level_atoms(path)
    assert atoms[0] == b"ftyp"
    assert atoms.index(b"mdat") < atoms.index(b"moov")

    # A local seek to the trailing index must support bounded metadata inspection.
    info = await inspect_media_info(imageio_ffmpeg.get_ffmpeg_exe(), path)
    assert info["status"] == "ready"
    assert info["codec"] == "h264"
    assert (info["width"], info["height"]) == (160, 120)
    assert (info["fps_num"], info["fps_den"]) == (10, 1)
    verified = await verify_archive(imageio_ffmpeg.get_ffmpeg_exe(), path, segments)
    assert verified["packet_count"] == 60
    assert verified["duration"] == pytest.approx(6, abs=0.01)
    assert verified["size_bytes"] == path.stat().st_size


@pytest.mark.asyncio
async def test_event_clip_from_trailing_index_archive_still_has_faststart(tmp_path):
    segments, archive = await prepared(tmp_path)
    (tmp_path / segments[0]["path"]).unlink()
    output = tmp_path / "clips" / "event.mp4"
    metadata = await remux_clip(
        imageio_ffmpeg.get_ffmpeg_exe(),
        tmp_path,
        [
            {
                "path": str(archive.relative_to(tmp_path)),
                "media_start": 0.0,
                "start_at": 1000.0,
                "end_at": 1006.0,
                "clip_start": 1001.35,
                "clip_end": 1004.25,
                "duration": 6.0,
                "profile": {"width": 160, "height": 120, "fps": 10},
            }
        ],
        output,
    )
    atoms = top_level_atoms(output)
    assert atoms.index(b"moov") < atoms.index(b"mdat")
    assert metadata["media_info"]["codec"] == "h264"
    assert 2.9 <= metadata["duration"] <= 4.9


@pytest.mark.asyncio
async def test_legacy_jpeg_clip_still_has_faststart(tmp_path):
    buffer = tmp_path / "buffer"
    output = tmp_path / "clips"
    buffer.mkdir()
    output.mkdir()
    jpeg = io.BytesIO()
    Image.new("RGB", (32, 32), "blue").save(jpeg, "JPEG")
    frames = []
    for index in range(3):
        name = f"{index}.jpg"
        (buffer / name).write_bytes(jpeg.getvalue())
        frames.append((name, 1000 + index / 10))
    await encode_clip(imageio_ffmpeg.get_ffmpeg_exe(), buffer, output, "legacy", frames)
    atoms = top_level_atoms(output / "legacy.mp4")
    assert atoms.index(b"moov") < atoms.index(b"mdat")
