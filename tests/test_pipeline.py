from __future__ import annotations

import pytest

from camera_media_service.pipeline import FrameValidator, LiveFrameHub
from camera_media_service.store import LatestFrameStore

VALID_JPEG = b"\xff\xd8\xff\xe0synthetic-jpeg\xff\xd9"


def test_only_recording_queue_drops_emit_loss_notification():
    hub = LiveFrameHub(LatestFrameStore())
    losses = []
    hub.loss_observer = lambda *args: losses.append(args)
    hub.subscribe(maxsize=1)  # Live viewer drops must not imply lost storage.
    hub.publish("cam-1", VALID_JPEG)
    hub.publish("cam-1", VALID_JPEG)
    assert losses == []
    hub.subscribe(maxsize=1, recording=True)
    dropped = hub.publish("cam-1", VALID_JPEG)
    hub.publish("cam-1", VALID_JPEG)
    assert len(losses) == 1 and losses[0][0] == "cam-1"
    assert losses[0][1] == dropped.received_at and losses[0][2] > dropped.received_at
    assert losses[0][3] == "BUFFER_DROPPED"


def test_frame_validator_detects_valid_and_invalid_jpeg() -> None:
    validator = FrameValidator(max_frame_bytes=100)

    assert validator.is_jpeg(VALID_JPEG) is True
    assert validator.is_jpeg(b"not-jpeg") is False
    assert validator.is_within_size(VALID_JPEG) is True
    assert validator.is_within_size(b"x" * 101) is False


def test_live_frame_hub_publishes_to_store_and_subscribers() -> None:
    store = LatestFrameStore()
    hub = LiveFrameHub(frame_store=store)
    queue = hub.subscribe(maxsize=10)

    envelope = hub.publish("cam-1", VALID_JPEG)

    assert envelope.camera_id == "cam-1"
    assert envelope.generation == 1
    assert envelope.jpeg == VALID_JPEG

    assert store.get("cam-1") == (1, VALID_JPEG)
    assert queue.qsize() == 1
    received = queue.get_nowait()
    assert received == envelope

    hub.unsubscribe(queue)
    assert hub.subscriber_count == 0


def test_live_frame_hub_drops_oldest_frame_when_subscriber_queue_is_full() -> None:
    store = LatestFrameStore()
    hub = LiveFrameHub(frame_store=store)
    queue = hub.subscribe(maxsize=2)

    hub.publish("cam-1", VALID_JPEG + b"1")
    env2 = hub.publish("cam-1", VALID_JPEG + b"2")
    env3 = hub.publish("cam-1", VALID_JPEG + b"3")

    assert queue.qsize() == 2
    items = [queue.get_nowait(), queue.get_nowait()]
    assert items == [env2, env3]


@pytest.mark.asyncio
async def test_recording_barrier_accounts_for_dropped_frames():
    import asyncio

    hub = LiveFrameHub(LatestFrameStore())
    queue = hub.subscribe(maxsize=1, recording=True)
    hub.publish("cam-1", VALID_JPEG)
    barrier = asyncio.create_task(queue.wait_received())
    await asyncio.sleep(0)
    hub.publish("cam-1", VALID_JPEG + b"next")
    await asyncio.wait_for(barrier, 0.5)
    assert queue.qsize() == 1
