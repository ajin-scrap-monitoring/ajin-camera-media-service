"""Bounded, durable pressure cleanup of explicitly eligible daily archives only."""

import asyncio
import json
import logging
import math
import sqlite3
import time

from camera_media_service.capacity_policy import CleanupPolicy, pressure_snapshot
from camera_media_service.catalog_pins import finish_cleanup
from camera_media_service.media_files import storage_io
from camera_media_service.recording import ApiError, epoch, iso

LOG = logging.getLogger(__name__)


class CapacityCleanup:
    def __init__(self, exports):
        self.exports = exports
        self.daily = exports.daily
        self.recording = self.daily.recording
        self.policy = getattr(exports.settings, "capacity_cleanup", CleanupPolicy())
        self.lock = asyncio.Lock()
        self.error = self.policy.configuration_error
        db = self.daily.db
        db.execute(
            "CREATE TABLE IF NOT EXISTS capacity_cleanup_state("
            "id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL)"
        )
        db.commit()
        self.state = dict(
            active=False,
            mode="idle",
            reason_code=None,
            observed_at=None,
            measurement=None,
            last_cycle_at=None,
            consecutive_failures=0,
            retry_at=0,
            attempts=0,
            successful_days=0,
            observed_reclaimed_bytes=0,
            pending=None,
            last_attempt=None,
            critical=False,
            alert_latched=False,
            notification_sequence=0,
            last_notification_at=0,
            last_progress_at=None,
            stalled=False,
            reset_id=self.policy.reset_id,
        )
        row = db.execute(
            "SELECT CASE WHEN octet_length(body)<=65536 THEN body END "
            "FROM capacity_cleanup_state WHERE id=1"
        ).fetchone()
        if row:
            try:
                saved = json.loads(row[0])
                if (
                    not isinstance(saved, dict)
                    or type(saved.get("active")) is not bool
                    or type(saved.get("consecutive_failures")) is not int
                    or not 0 <= saved["consecutive_failures"] <= 100
                    or (saved.get("pending") is not None and not isinstance(saved["pending"], dict))
                ):
                    raise ValueError()
                self._validate_saved(saved)
                self.state.update({k: v for k, v in saved.items() if k in self.state})
                if self.policy.reset_id and self.policy.reset_id != saved.get("reset_id"):
                    self.state.update(
                        consecutive_failures=0, retry_at=0, reset_id=self.policy.reset_id
                    )
            except (ValueError, TypeError, AttributeError, OverflowError):
                self.error = "CLEANUP_STATE_UNAVAILABLE"

    @staticmethod
    def _validate_saved(saved):
        json.dumps(saved, allow_nan=False)
        for key in ("critical", "alert_latched", "active"):
            if type(saved.get(key)) is not bool:
                raise ValueError()
        for key in (
            "attempts",
            "successful_days",
            "observed_reclaimed_bytes",
            "notification_sequence",
        ):
            if type(saved.get(key)) is not int or saved[key] < 0:
                raise ValueError()
        for key in ("last_cycle_at", "retry_at", "last_notification_at", "last_progress_at"):
            value = saved.get(key)
            if value is None and key in ("last_cycle_at", "last_progress_at"):
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError()
        if not isinstance(saved.get("reset_id"), str) or len(saved["reset_id"]) > 64:
            raise ValueError()
        if saved.get("last_attempt") is not None and not isinstance(saved["last_attempt"], dict):
            raise ValueError()
        pending = saved.get("pending")
        if pending is not None:
            if (
                not isinstance(pending.get("recording_id"), str)
                or type(pending.get("bytes")) is not int
                or pending["bytes"] < 0
            ):
                raise ValueError()
            if type(pending.get("files_counted", False)) is not bool:
                raise ValueError()
            pressure_snapshot({"status": "available", "budget": pending.get("before")})
        measurement = saved.get("measurement")
        if measurement is not None:
            if not isinstance(measurement, dict):
                raise ValueError()
            pressure_snapshot({"status": "available", "budget": measurement.get("budget")})
        observed = saved.get("observed_at")
        if observed is not None and (
            not isinstance(observed, str) or not math.isfinite(epoch(observed))
        ):
            raise ValueError()

    def status(self):
        result = json.loads(json.dumps(self.state))
        result.pop("reset_id", None)
        result.update(
            enabled=self.policy.enabled and not self.error,
            configuration_error=self.error,
            policy=self.policy.public(),
            scope="closed_ready_daily_mp4",
            default_retention_days=30,
            minimum_retention_guaranteed=False,
        )
        if not result["enabled"]:
            result["mode"] = "disabled"
        observed = result["observed_at"]
        valid_until = epoch(observed) + self.policy.interval_seconds * 2 + 10 if observed else None
        result.update(
            valid_until_at=iso(valid_until) if valid_until else None,
            state_fresh=valid_until is not None and time.time() <= valid_until,
        )
        return result

    async def _save(self):
        body = json.dumps(self.state, allow_nan=False)
        if len(body.encode()) > 65536:
            raise ValueError("Cleanup state limit exceeded")
        async with self.recording.lock:
            async with self.recording.transaction():
                await storage_io(
                    self.daily.db.execute,
                    "INSERT OR REPLACE INTO capacity_cleanup_state VALUES(1,?)",
                    (body,),
                )

    def _notice(self, now):
        s = self.state
        if s["critical"] and (
            not s["alert_latched"]
            or now - s["last_notification_at"] >= self.policy.notification_seconds
        ):
            s.update(
                alert_latched=True,
                last_notification_at=now,
                notification_sequence=s["notification_sequence"] + 1,
            )
            LOG.warning(
                "capacity_cleanup_critical %s",
                json.dumps(
                    {
                        "sequence": s["notification_sequence"],
                        "reason_code": s["reason_code"],
                        "pressures": s["measurement"]["pressures"],
                    }
                ),
            )
        elif s["alert_latched"] and all(
            p["used_bytes"] * 100 <= p["limit_bytes"] * self.policy.stop_percent
            for p in s["measurement"]["pressures"].values()
        ):
            s.update(
                alert_latched=False,
                notification_sequence=s["notification_sequence"] + 1,
                last_notification_at=now,
            )
            LOG.info("capacity_cleanup_recovered sequence=%s", s["notification_sequence"])

    async def _measure(self, now):
        self.recording.storage_guard()
        measured = await self.daily.inspect_capacity(recording=True)
        value = pressure_snapshot(measured)
        self.state.update(
            measurement=value,
            observed_at=measured.get("observed_at") or iso(now),
            critical=any(
                p["used_bytes"] * 100 >= p["limit_bytes"] * self.policy.critical_percent
                for p in value["pressures"].values()
            ),
        )
        return value

    def _below_stop(self):
        return all(
            p["used_bytes"] * 100 <= p["limit_bytes"] * self.policy.stop_percent
            for p in self.state["measurement"]["pressures"].values()
        )

    async def tick(self, now):
        if self.lock.locked() or not self.policy.enabled or self.error:
            return
        async with self.lock:
            previous = self.state["last_cycle_at"]
            if previous is not None and previous <= now < previous + self.policy.interval_seconds:
                return
            try:
                await self._tick(now)
            except (OSError, sqlite3.Error, ValueError, ApiError):
                self.state.update(
                    mode="error",
                    reason_code="MEASUREMENT_UNAVAILABLE",
                    observed_at=None,
                    measurement=None,
                )
                LOG.exception("Capacity cleanup stopped: unavailable guarded state")
            finally:
                self.state["last_cycle_at"] = now
                self.state["stalled"] = bool(
                    self.state["active"]
                    and self.state["last_progress_at"] is not None
                    and now - self.state["last_progress_at"] >= self.policy.stall_seconds
                )
                try:
                    if self.state["measurement"] is not None:
                        self._notice(now)
                    await finish_cleanup(self._save())
                except (OSError, sqlite3.Error, ValueError):
                    self.error = "CLEANUP_STATE_UNAVAILABLE"
                    LOG.exception("Capacity cleanup disabled: state could not be persisted")

    async def _tick(self, now):
        s = self.state
        await self._measure(now)
        if any(
            p["used_bytes"] * 100 >= p["limit_bytes"] * self.policy.start_percent
            for p in s["measurement"]["pressures"].values()
        ):
            if not s["active"]:
                s["last_progress_at"] = now
            s["active"] = True
        if s["pending"] is not None:
            intent = await self.exports.pressure_intent_state(s["pending"]["recording_id"])
            if intent == "obsolete" or (
                intent == "uncommitted" and (self._below_stop() or not s["active"])
            ):
                s["pending"] = None
        if self._below_stop():
            s["active"] = False
        if s["consecutive_failures"] >= self.policy.max_failures:
            s.update(mode="suspended", reason_code="FAILURE_LIMIT_REACHED")
            return
        if now < s["retry_at"]:
            s["mode"] = "cooldown"
            return
        if not s["pending"] and (not s["active"] or self._below_stop()):
            s.update(active=False, mode="idle", reason_code=None)
            return
        s.update(mode="active", reason_code=None)
        completed = used_bytes = 0
        while completed < self.policy.max_days:
            if not s["pending"] and self._below_stop():
                s.update(active=False, mode="idle", reason_code=None)
                break
            candidate = s["pending"]
            if candidate is None:
                candidate, reason = await self.exports.pressure_candidate(
                    self.policy.max_bytes - used_bytes
                )
                if candidate is None:
                    s.update(mode="blocked", reason_code=reason)
                    break
                if candidate["bytes"] > self.policy.max_bytes - used_bytes:
                    s.update(mode="blocked", reason_code="TARGET_EXCEEDS_LIMIT")
                    break
                s["pending"] = {**candidate, "before": s["measurement"]["budget"]}
                candidate = s["pending"]
                await self._save()
            if candidate["bytes"] > self.policy.max_bytes - used_bytes:
                s.update(mode="blocked", reason_code="TARGET_EXCEEDS_LIMIT")
                break
            s["attempts"] += 1
            s["last_attempt"] = {
                "recording_id": candidate["recording_id"],
                "attempted_at": iso(now),
                "expected_bytes": candidate["bytes"],
                "success": False,
                "space_recovery_confirmed": False,
            }
            already_removed = (
                await self.exports.pressure_intent_state(candidate["recording_id"]) == "complete"
            )
            s["last_attempt"]["phase"] = "reclaim_check" if already_removed else "delete"
            await self._save()
            try:
                if not already_removed:
                    await self.exports.delete_day(
                        candidate["recording_id"],
                        reason="capacity_pressure",
                        max_bytes=self.policy.max_bytes - used_bytes,
                    )
            except ApiError as exc:
                if exc.status == 409:
                    intent = await self.exports.pressure_intent_state(candidate["recording_id"])
                    if intent in ("uncommitted", "obsolete"):
                        s.update(mode="blocked", reason_code=exc.code, pending=None)
                    else:
                        self._failure(now, exc.code)
                    break
                self._failure(now, "DELETE_ERROR")
                break
            except OSError:
                self._failure(now, "DELETE_ERROR")
                break
            before = candidate["before"]
            await self._measure(now)
            after = s["measurement"]["budget"]
            free_gain = max(0, after["filesystem_free_bytes"] - before["filesystem_free_bytes"])
            daily_drop = max(0, before["daily_used_bytes"] - after["daily_used_bytes"])
            growth = max(
                0, after["media_used_bytes"] - before["media_used_bytes"] + candidate["bytes"]
            )
            confirmed = (
                free_gain > 0
                and daily_drop > 0
                and (free_gain + growth) * 100 >= candidate["bytes"] * 95
            )
            s["last_attempt"].update(
                success=True,
                space_recovery_confirmed=confirmed,
                observed_filesystem_free_increase_bytes=free_gain,
                observed_daily_usage_decrease_bytes=daily_drop,
                estimated_other_growth_bytes=growth,
                required_recovery_bytes=(candidate["bytes"] * 95 + 99) // 100,
                completed_at=iso(now),
            )
            if not candidate.get("files_counted", False):
                s["successful_days"] += 1
                candidate["files_counted"] = True
            completed += 1
            used_bytes += candidate["bytes"]
            if not confirmed:
                self._failure(
                    now,
                    "GENERATION_OUTPACES_CLEANUP"
                    if growth >= candidate["bytes"]
                    else "SPACE_NOT_RECLAIMED",
                )
                break
            s.update(
                pending=None, observed_reclaimed_bytes=s["observed_reclaimed_bytes"] + free_gain
            )
            s.update(consecutive_failures=0, retry_at=0, last_progress_at=now)
            await self._save()
        if not s["pending"] and self._below_stop():
            s.update(active=False, mode="idle", reason_code=None)
        elif s["reason_code"] is None:
            s["reason_code"] = "CYCLE_LIMIT_REACHED"
        s["stalled"] = bool(
            s["active"]
            and s["last_progress_at"] is not None
            and now - s["last_progress_at"] >= self.policy.stall_seconds
        )

    def _failure(self, now, reason):
        s = self.state
        failures = s["consecutive_failures"] + 1
        s.update(
            consecutive_failures=failures,
            mode="suspended" if failures >= self.policy.max_failures else "cooldown",
            reason_code=reason,
            retry_at=now + min(900, self.policy.retry_seconds * 2 ** (failures - 1)),
        )
        s["last_attempt"]["error_code"] = reason
        LOG.warning("capacity_cleanup_failed reason=%s failures=%s", reason, failures)
