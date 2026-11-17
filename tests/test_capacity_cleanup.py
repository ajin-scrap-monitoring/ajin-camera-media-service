import asyncio
import importlib.util
import json
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from camera_media_service.capacity_policy import CleanupPolicy


def measurement(used, free=1000):
    return {
        "status": "available",
        "budget": dict(
            daily_used_bytes=used,
            media_used_bytes=used,
            filesystem_free_bytes=free,
            filesystem_total_bytes=2000,
            unallocated_reserved_bytes=0,
            additional_reserve_bytes=0,
            workspace_bytes=0,
            buffer_headroom_bytes=0,
            daily_limit_bytes=1000,
            media_limit_bytes=2000,
            minimum_free_bytes=0,
        ),
    }


class Fixture:
    def __init__(self, tmp_path):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = asyncio.Lock()
        self.root = tmp_path
        self.budget = measurement(910)
        self.days = [
            {"recording_id": "oldest", "bytes": 100},
            {"recording_id": "newer", "bytes": 100},
        ]
        self.deleted = []
        self.settings = SimpleNamespace(
            capacity_cleanup=CleanupPolicy(
                enabled=True, interval_seconds=5, max_days=1, retry_seconds=5, max_failures=2
            )
        )
        self.recording = self
        self.daily = self

    @asynccontextmanager
    async def transaction(self):
        with self.db:
            yield

    async def inspect_capacity(self, **kwargs):
        return self.budget

    def storage_guard(self):
        pass

    async def pressure_candidate(self, limit):
        if not self.days:
            return None, "TARGETS_EXHAUSTED"
        return self.days[0], None

    async def delete_day(self, key, **kwargs):
        self.deleted.append(key)
        self.days.pop(0)
        self.budget = measurement(
            self.budget["budget"]["daily_used_bytes"] - 100,
            self.budget["budget"]["filesystem_free_bytes"] + 100,
        )


def cleaner(f):
    assert importlib.util.find_spec("camera_media_service.capacity_cleanup"), (
        "cleanup worker missing"
    )
    from camera_media_service.capacity_cleanup import CapacityCleanup

    return CapacityCleanup(f)


@pytest.mark.asyncio
async def test_hysteresis_durable_state_and_bounded_deletions(tmp_path):
    f = Fixture(tmp_path)
    c = cleaner(f)
    await c.tick(100)
    assert f.deleted == ["oldest"] and not c.status()["active"]
    # Enter again but stop quota above85; restart must keep active at88.
    f.budget = measurement(980)
    f.days = [{"recording_id": "a", "bytes": 100}, {"recording_id": "b", "bytes": 100}]
    await c.tick(110)
    assert c.status()["active"] and f.budget["budget"]["daily_used_bytes"] == 880
    c = cleaner(f)
    await c.tick(120)
    assert f.deleted[-1] == "b" and not c.status()["active"]
    assert c.status()["successful_days"] == 3


@pytest.mark.asyncio
async def test_no_deletion_below_start_disabled_or_bad_measurement(tmp_path):
    f = Fixture(tmp_path)
    f.budget = measurement(899)
    c = cleaner(f)
    await c.tick(100)
    assert not f.deleted
    f.budget = {"status": "unknown"}
    await c.tick(110)
    assert not f.deleted and c.status()["reason_code"] == "MEASUREMENT_UNAVAILABLE"
    f.settings.capacity_cleanup = CleanupPolicy()
    d = cleaner(f)
    await d.tick(120)
    assert not f.deleted and not d.status()["enabled"]


@pytest.mark.asyncio
async def test_retry_budget_survives_restart_and_requires_explicit_reset(tmp_path):
    f = Fixture(tmp_path)

    async def fail(*args, **kwargs):
        raise OSError("disk failed")

    f.delete_day = fail
    c = cleaner(f)
    await c.tick(100)
    assert c.status()["consecutive_failures"] == 1
    await c.tick(101)
    assert c.status()["consecutive_failures"] == 1
    c = cleaner(f)
    await c.tick(110)
    assert c.status()["consecutive_failures"] == 2 and c.status()["mode"] == "suspended"
    await c.tick(2000)
    assert c.status()["attempts"] == 2
    f.settings.capacity_cleanup = replace(f.settings.capacity_cleanup, reset_id="operator-reset")
    c = cleaner(f)
    await c.tick(2100)
    assert c.status()["attempts"] == 3


@pytest.mark.asyncio
async def test_no_recovery_stops_cycle_and_reports_critical_with_dedupe(tmp_path):
    f = Fixture(tmp_path)
    f.budget = measurement(970)

    async def no_recovery(key, **kwargs):
        f.deleted.append(key)
        f.days.pop(0)

    f.delete_day = no_recovery
    c = cleaner(f)
    await c.tick(100)
    assert len(f.deleted) == 1 and c.status()["critical"]
    assert c.status()["last_attempt"]["space_recovery_confirmed"] is False
    sequence = c.status()["notification_sequence"]
    await c.tick(101)
    assert c.status()["notification_sequence"] == sequence
    assert c.status()["reason_code"] in ("GENERATION_OUTPACES_CLEANUP", "SPACE_NOT_RECLAIMED")


@pytest.mark.asyncio
async def test_over_limit_day_and_protected_targets_are_not_deleted(tmp_path):
    f = Fixture(tmp_path)
    f.settings.capacity_cleanup = replace(f.settings.capacity_cleanup, max_bytes=50)
    c = cleaner(f)
    await c.tick(100)
    assert not f.deleted and c.status()["reason_code"] == "TARGET_EXCEEDS_LIMIT"

    async def blocked(limit):
        return None, "PROTECTED_TARGETS_ONLY"

    f.pressure_candidate = blocked
    await c.tick(110)
    assert not f.deleted and c.status()["reason_code"] == "PROTECTED_TARGETS_ONLY"


@pytest.mark.asyncio
async def test_single_runner_does_not_duplicate_deletion(tmp_path):
    f = Fixture(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = f.delete_day

    async def slow(*args, **kwargs):
        entered.set()
        await release.wait()
        await original(*args, **kwargs)

    f.delete_day = slow
    c = cleaner(f)
    task = asyncio.create_task(c.tick(100))
    await asyncio.wait_for(entered.wait(), 1)
    await c.tick(100)
    release.set()
    await task
    assert f.deleted == ["oldest"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("retry_at", "bad"),
        ("attempts", False),
        ("last_cycle_at", float("nan")),
        ("critical", 1),
        ("pending", {"recording_id": "oldest", "bytes": -1, "before": {}}),
    ],
)
async def test_corrupt_durable_state_disables_only_cleanup(tmp_path, field, value):
    f = Fixture(tmp_path)
    c = cleaner(f)
    await c.tick(100)
    raw = json.loads(
        f.db.execute("SELECT body FROM capacity_cleanup_state WHERE id=1").fetchone()[0]
    )
    raw[field] = value
    f.db.execute("UPDATE capacity_cleanup_state SET body=?", (json.dumps(raw),))
    f.db.commit()
    c = cleaner(f)
    assert (
        not c.status()["enabled"]
        and c.status()["configuration_error"] == "CLEANUP_STATE_UNAVAILABLE"
    )


@pytest.mark.asyncio
async def test_critical_recovery_and_repeat_are_durable(tmp_path):
    f = Fixture(tmp_path)
    f.days = []
    f.budget = measurement(970)
    c = cleaner(f)
    await c.tick(100)
    assert c.status()["notification_sequence"] == 1
    c = cleaner(f)
    await c.tick(110)
    assert c.status()["notification_sequence"] == 1
    await c.tick(1001)
    assert c.status()["notification_sequence"] == 2
    f.budget = measurement(850)
    await c.tick(1010)
    assert (
        not c.status()["active"]
        and not c.status()["alert_latched"]
        and c.status()["notification_sequence"] == 3
    )
