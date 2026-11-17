"""Explicit opt-in policy and ratios using the recorder's admission snapshot."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CleanupPolicy:
    enabled: bool = False
    stop_percent: int = 85
    start_percent: int = 90
    critical_percent: int = 95
    interval_seconds: int = 30
    max_days: int = 1
    max_bytes: int = 16 * 1024**3
    retry_seconds: int = 60
    max_failures: int = 3
    stall_seconds: int = 300
    notification_seconds: int = 900
    reset_id: str = ""
    configuration_error: str | None = None

    @classmethod
    def from_env(cls, env):
        prefix = "MEDIA_CAPACITY_CLEANUP_"
        try:
            enabled = env.get(prefix + "ENABLED", "false").strip().lower()
            if enabled not in ("true", "false"):
                raise ValueError()
            values = {"enabled": enabled == "true"}
            bounds = dict(
                stop_percent=(1, 99),
                start_percent=(2, 99),
                critical_percent=(3, 100),
                interval_seconds=(5, 3600),
                max_days=(1, 100),
                max_bytes=(1, 1024**4),
                retry_seconds=(5, 900),
                max_failures=(1, 10),
                stall_seconds=(30, 86400),
                notification_seconds=(60, 86400),
            )
            allowed = {prefix + k.upper() for k in (*bounds, "enabled", "reset_id")}
            if any(k.startswith(prefix) and k not in allowed for k in env):
                raise ValueError()
            defaults = cls()
            for key, (low, high) in bounds.items():
                value = int(env.get(prefix + key.upper(), str(getattr(defaults, key))))
                if not low <= value <= high:
                    raise ValueError()
                values[key] = value
            if not values["stop_percent"] < values["start_percent"] < values["critical_percent"]:
                raise ValueError()
            reset = env.get(prefix + "RESET_ID", "").strip()
            if len(reset) > 64 or any(ord(c) < 32 for c in reset):
                raise ValueError()
            return cls(**values, reset_id=reset)
        except (ValueError, TypeError, AttributeError):
            return cls(configuration_error="INVALID_CAPACITY_CLEANUP_CONFIG")

    def public(self):
        return {k: v for k, v in asdict(self).items() if k != "reset_id"}


def pressure_snapshot(measurement):
    if not isinstance(measurement, dict) or measurement.get("status") not in (
        "available",
        "insufficient_capacity",
    ):
        raise ValueError("Capacity measurement unavailable")
    b = measurement.get("budget")
    if not isinstance(b, dict):
        raise ValueError("Capacity budget unavailable")
    keys = (
        "daily_used_bytes",
        "media_used_bytes",
        "filesystem_free_bytes",
        "filesystem_total_bytes",
        "unallocated_reserved_bytes",
        "additional_reserve_bytes",
        "workspace_bytes",
        "buffer_headroom_bytes",
        "daily_limit_bytes",
        "minimum_free_bytes",
    )
    if any(type(b.get(k)) is not int or b[k] < 0 for k in keys):
        raise ValueError("Invalid capacity budget")
    if (
        not b["daily_limit_bytes"]
        or not b["filesystem_total_bytes"]
        or b["filesystem_free_bytes"] > b["filesystem_total_bytes"]
    ):
        raise ValueError("Invalid capacity limits")
    reserve = b["unallocated_reserved_bytes"] + b["additional_reserve_bytes"]
    future = reserve + b["workspace_bytes"]
    pairs = {
        "daily_budget": (b["daily_used_bytes"] + future, b["daily_limit_bytes"]),
        "minimum_free": (
            b["filesystem_total_bytes"]
            - b["filesystem_free_bytes"]
            + future
            + b["buffer_headroom_bytes"]
            + b["minimum_free_bytes"],
            b["filesystem_total_bytes"],
        ),
    }
    limit = b.get("media_limit_bytes")
    if limit is not None:
        if type(limit) is not int or limit <= 0:
            raise ValueError("Invalid media limit")
        pairs["total_budget"] = (b["media_used_bytes"] + future + b["buffer_headroom_bytes"], limit)
    return {
        "budget": dict(b),
        "pressures": {
            k: {
                "used_bytes": used,
                "limit_bytes": limit,
                "percent": round(100 * used / limit, 6),
                "shortfall_bytes": max(0, used - limit),
            }
            for k, (used, limit) in pairs.items()
        },
    }
