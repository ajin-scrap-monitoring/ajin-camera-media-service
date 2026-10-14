"""Intersect missing coverage with loss reasons without a gap-by-segment Cartesian scan."""

import heapq
from itertools import groupby

from camera_media_service.recording import iso


def build_gaps(spans, known):
    events = []
    for priority, row in enumerate(known):
        if row["end"] > row["start"]:
            events.extend(
                (
                    (row["start"], 1, priority, row["reason"]),
                    (row["end"], 0, priority, row["reason"]),
                )
            )
    effective = []
    active, heap = set(), []
    previous = None
    for at, batch in groupby(sorted(events), key=lambda event: event[0]):
        if previous is not None and at > previous and heap:
            reason = heap[0][1]
            if effective and effective[-1][2] == reason and effective[-1][1] == previous:
                effective[-1] = (effective[-1][0], at, reason)
            else:
                effective.append((previous, at, reason))
        for _, beginning, priority, reason in batch:
            if beginning:
                active.add(priority)
                heapq.heappush(heap, (-priority, reason))
            else:
                active.discard(priority)
        while heap and -heap[0][0] not in active:
            heapq.heappop(heap)
        previous = at
    gaps, cursor = [], 0

    def append(start, end, reason):
        left, right = iso(start), iso(end)
        if gaps and gaps[-1]["reason_code"] == reason and gaps[-1]["end_at"] == left:
            gaps[-1]["end_at"] = right
        else:
            gaps.append({"start_at": left, "end_at": right, "reason_code": reason})

    for a, b in spans:
        if b <= a:
            continue
        while cursor < len(effective) and effective[cursor][1] <= a:
            cursor += 1
        index, point = cursor, a
        while index < len(effective) and effective[index][0] < b:
            left, right, reason = effective[index]
            left, right = max(a, left), min(b, right)
            if left - point > 1:
                append(point, left, "UNKNOWN_CAPTURE_LOSS")
            if right > left:
                append(left, right, reason)
                point = right
            index += 1
        if b - point > 1:
            append(point, b, "UNKNOWN_CAPTURE_LOSS")
    return gaps
