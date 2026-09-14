from __future__ import annotations

from camera_media_service.store import LatestFrameStore


def test_latest_frame_store_replaces_per_camera_frame_and_defensively_copies_bytes() -> None:
    store = LatestFrameStore()
    frame = bytearray(b"\xff\xd8frame-a\xff\xd9")

    generation_1 = store.put("cam-1", frame)
    frame[:] = b"\xff\xd8changed\xff\xd9"

    generation_2 = store.put("cam-1", b"\xff\xd8frame-b\xff\xd9")
    other_generation = store.put("cam-2", b"\xff\xd8other\xff\xd9")

    assert generation_1 == 1
    assert generation_2 == 2
    assert other_generation == 1
    assert store.get("cam-1") == (2, b"\xff\xd8frame-b\xff\xd9")
    assert store.get("cam-2") == (1, b"\xff\xd8other\xff\xd9")


def test_latest_frame_store_returns_none_for_unknown_camera() -> None:
    store = LatestFrameStore()

    assert store.get("missing-camera") is None


def test_latest_frame_store_returns_a_new_copy_for_each_read() -> None:
    store = LatestFrameStore()
    store.put("cam-1", b"\xff\xd8frame\xff\xd9")

    first = store.get("cam-1")
    second = store.get("cam-1")

    assert first == (1, b"\xff\xd8frame\xff\xd9")
    assert second == (1, b"\xff\xd8frame\xff\xd9")
    assert first is not None
    assert second is not None
    assert first[1] is not second[1]
