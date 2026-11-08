"""Select compressed source JPEGs on the daily output's receipt-time grid."""

import math


def select_daily_frames(frames, fps, end_at):
    if not frames:
        return []
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("Daily fps must be positive and finite")
    start = frames[0].received_at
    count = max(1, math.ceil((end_at - start) * fps - 0.000001))
    selected = [frames[0]]
    index = last = 0
    for number in range(1, count):
        target = math.nextafter(start + number / fps, math.inf)
        while index + 1 < len(frames) and frames[index + 1].received_at <= target:
            index += 1
        if index != last:
            selected.append(frames[index])
            last = index
    return selected
