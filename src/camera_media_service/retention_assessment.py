"""Read-only lower-bound planning; this never changes retention or deletes media."""

import math


def assess_retention(
    policies, samples, limits, *, workspace, buffer, minimum_free, filesystem_total
):
    cameras = []
    known = True
    required = 0
    for camera, policy in sorted(policies.items()):
        sample = samples.get(camera)
        daily_bytes = None
        if sample and policy["enabled"]:
            seconds, size = sample["recorded_seconds"], sample["size_bytes"]
            if (
                isinstance(seconds, int | float)
                and math.isfinite(seconds)
                and 0.9 * 86400 <= seconds <= 1.1 * 86400
                and type(size) is int
                and size > 0
            ):
                daily_bytes = math.ceil(size * 86400 / seconds)
        if not policy["enabled"]:
            daily_bytes = 0
        if daily_bytes is None:
            known = False
        else:
            required += daily_bytes * policy["retention_days"]
        cameras.append(
            {
                "camera_id": camera,
                **policy,
                "sample_recording_id": (sample["recording_id"] if sample and daily_bytes else None),
                "estimated_daily_bytes": daily_bytes,
                "estimated_retention_bytes": (
                    daily_bytes * policy["retention_days"] if daily_bytes is not None else None
                ),
            }
        )
    enabled = any(p["enabled"] for p in policies.values())
    working = workspace if enabled else 0
    daily = required + working
    media = daily + buffer
    filesystem = media + minimum_free
    conditions = []
    for name, need, limit in (
        ("daily_budget", daily, limits.get("daily_limit_bytes")),
        ("media_budget", media, limits.get("media_limit_bytes")),
        ("filesystem", filesystem, filesystem_total),
    ):
        if limit is not None and need > limit:
            conditions.append(name)
    return {
        "status": "insufficient_capacity"
        if conditions
        else "estimated_fit"
        if known
        else "unknown",
        "basis": "recent_near_full_day_mp4_per_recorded_second",
        "cameras": cameras,
        "estimated_retention_bytes": required if known else None,
        "required_daily_budget_bytes_lower_bound": daily if known else None,
        "required_media_budget_bytes_lower_bound": media if known else None,
        "required_filesystem_bytes_lower_bound": filesystem if known else None,
        "limiting_conditions": conditions,
        "archive_workspace_floor_bytes": workspace,
        "buffer_limit_bytes": buffer,
        "minimum_free_bytes": minimum_free,
        "includes_clip_catalog_and_playback_cache_growth": False,
        "policy_changed": False,
    }
