import io
import json

import pytest


class BoundedInput(io.BytesIO):
    def read(self, size=-1):
        assert 0 < size <= 65536, "Snapshot input must use bounded reads"
        return super().read(size)


@pytest.mark.parametrize("chunk_bytes", [1, 3, 17, 65536])
def test_utf8_arrays_retain_seekable_byte_offsets(chunk_bytes):
    from camera_media_service.snapshot_stream import SnapshotJSON

    values = [{"text": "한글 🌊", "number": -12.34e-7}, {"text": '\\"끝', "number": 1}]
    encoded = json.dumps({"label": "날짜", "items": values}, ensure_ascii=False).encode()
    reader = SnapshotJSON(BoundedInput(encoded), chunk_bytes=chunk_bytes)
    items = []
    for key in reader.object_keys():
        if key == "items":
            items = list(reader.array_offsets())
        else:
            assert reader.value() == "날짜"
    reader.finish()
    assert [value for _, value in items] == values
    for offset, value in items:
        stream = BoundedInput(encoded)
        stream.seek(offset)
        assert SnapshotJSON(stream).value() == value


def test_private_large_arrays_and_strings_are_skipped_without_materializing():
    from camera_media_service.snapshot_stream import SnapshotJSON

    encoded = json.dumps(
        {
            "private": {"segments": [{"secret": "x" * 10000}] * 100},
            "private_text": '\\"\n' * 100000,
            "recording_date": "2026-11-14",
        }
    ).encode()
    reader = SnapshotJSON(BoundedInput(encoded), chunk_bytes=1024, max_value_bytes=2048)
    selected = {}
    for key in reader.object_keys():
        if key == "recording_date":
            selected[key] = reader.value()
        else:
            reader.skip()
    reader.finish()
    assert selected == {"recording_date": "2026-11-14"}
    assert reader.peak_buffer_bytes < 4096


@pytest.mark.parametrize(
    "encoded", [b'{"a":[1,]}', b'{"a":01}', b'{"a":"bad\x01"}', b'{"a":true', b"{} false"]
)
def test_malformed_snapshot_is_rejected(encoded):
    from camera_media_service.snapshot_stream import SnapshotJSON, SnapshotJSONError

    reader = SnapshotJSON(BoundedInput(encoded), chunk_bytes=3)
    with pytest.raises(SnapshotJSONError):
        for _ in reader.object_keys():
            reader.skip()
        reader.finish()


def test_selected_value_limit_does_not_silently_truncate():
    from camera_media_service.snapshot_stream import SnapshotJSON, SnapshotJSONError

    reader = SnapshotJSON(
        BoundedInput(json.dumps("x" * 100).encode()), chunk_bytes=16, max_value_bytes=32
    )
    with pytest.raises(SnapshotJSONError, match="limit"):
        reader.value()
