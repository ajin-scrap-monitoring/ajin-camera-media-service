"""Advisory policy validation; only an explicit PUT applies the requested policy."""

import asyncio
import sqlite3
import time

from camera_media_service.catalog_pins import finish_cleanup
from camera_media_service.daily_queries import DailySnapshots
from camera_media_service.media_files import storage_io
from camera_media_service.recording import ApiError, iso, validate_policy
from camera_media_service.retention_assessment import assess_retention
from camera_media_service.snapshot_stream import SnapshotJSONError


class PolicyCapacity:
    def __init__(self, recording, daily, storage_status):
        self.recording, self.daily, self.storage_status = recording, daily, storage_status
        self.reader = DailySnapshots(recording.root / "catalog.sqlite3") if daily else None
        self.slot = asyncio.Semaphore(1)
        self.admitted = 0

    async def _current(self, kind):
        try:
            if self.daily is not None:
                return await self.daily.inspect_capacity(recording=kind == "daily")
            await finish_cleanup(self.recording.check_media_space())
            return {
                "status": "available",
                "observed_at": iso(time.time()),
                "budget": None,
                "available_recording_bytes": None,
                "limiting_conditions": [],
                "reason_code": None,
            }
        except ApiError as exc:
            if exc.code != "INSUFFICIENT_STORAGE":
                return self._unknown_current(exc.code)
            return {
                "status": "insufficient_capacity",
                "observed_at": iso(time.time()),
                "budget": getattr(exc, "budget", None),
                "available_recording_bytes": 0,
                "limiting_conditions": [getattr(exc, "condition", "media_storage")],
                "reason_code": exc.code,
            }
        except (OSError, sqlite3.Error):
            return self._unknown_current("STORAGE_UNAVAILABLE")

    @staticmethod
    def _unknown_current(reason):
        return {
            "status": "unknown",
            "observed_at": None,
            "budget": None,
            "available_recording_bytes": None,
            "limiting_conditions": [],
            "reason_code": reason,
        }

    async def _retention(self, camera, proposed):
        async with self.daily.lock:
            policies = {key: self.daily._policy(key) for key in self.recording.cameras}
        policies[camera] = dict(proposed)
        try:

            def samples():
                self.recording.storage_guard()
                return self.reader.retention_samples(policies)

            measured = await storage_io(samples)
            storage = await finish_cleanup(self.storage_status.snapshot())
        except (OSError, sqlite3.Error, SnapshotJSONError):
            measured, storage = {}, {"status": "unavailable"}
        settings = self.daily.settings
        result = assess_retention(
            policies,
            measured,
            storage,
            workspace=getattr(settings, "daily_archive_workspace_bytes", 0),
            buffer=self.recording.max_buffer_bytes,
            minimum_free=settings.min_free_storage_bytes,
            filesystem_total=storage.get("filesystem_total_bytes"),
        )
        result.update(observed_at=storage.get("observed_at"), storage_status=storage["status"])
        if storage["status"] != "ready":
            result["status"] = "unknown"
        return result

    async def evaluate(self, camera, proposed, *, kind, apply=False):
        self.recording._camera(camera)
        if kind == "daily":
            from camera_media_service.daily_recording import validate_daily_policy

            policy = validate_daily_policy(proposed)
        elif kind == "clip":
            policy = validate_policy(proposed)
        else:
            raise ValueError("Unsupported policy kind")
        if self.admitted >= 4:
            raise ApiError(503, "SERVICE_BUSY", "Too many policy checks; retry later.")
        self.admitted += 1
        try:
            async with self.slot:
                current = await self._current(kind)
                warnings = []
                status = "unknown"
                retention = None
                if kind == "daily":
                    retention = await self._retention(camera, policy)
                    status = retention["status"]
                    if status == "insufficient_capacity":
                        warnings.append(
                            {
                                "code": "RETENTION_CAPACITY_EXCEEDED",
                                "message": "Estimated retention exceeds configured capacity.",
                                "conditions": retention["limiting_conditions"],
                            }
                        )
                    elif status == "unknown":
                        warnings.append(
                            {
                                "code": "CAPACITY_ESTIMATE_UNAVAILABLE",
                                "message": "Retention capacity cannot be estimated reliably.",
                            }
                        )
                else:
                    warnings.append(
                        {
                            "code": "CLIP_RETENTION_ESTIMATE_UNAVAILABLE",
                            "message": "Clip retention needs event frequency and size evidence.",
                        }
                    )
                if current["status"] == "insufficient_capacity":
                    status = "insufficient_capacity"
                    warnings.append(
                        {
                            "code": "CURRENT_STORAGE_INSUFFICIENT",
                            "message": "Current storage has no safe recording headroom.",
                            "conditions": current["limiting_conditions"],
                        }
                    )
                elif current["status"] == "unknown":
                    if status != "insufficient_capacity":
                        status = "unknown"
                    warnings.append(
                        {
                            "code": "STORAGE_OBSERVATION_UNAVAILABLE",
                            "message": "Current recording headroom could not be verified.",
                        }
                    )
                validation = {
                    "status": status,
                    "evaluated_at": iso(time.time()),
                    "policy_applied": False,
                    "warnings": warnings,
                    "current": current,
                    "retention": retention,
                    "reserves_space": False,
                    "existing_recordings_modified": False,
                }
                if apply:
                    if kind == "daily":
                        await self.daily.set_policy(camera, policy)
                    else:
                        await self.recording.set_policy(camera, policy)
                    validation["policy_applied"] = True
                return {**policy, "capacity_validation": validation}
        finally:
            self.admitted -= 1
