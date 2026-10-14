"""Korean calendar boundaries, independent of the host timezone."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

SEOUL = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True, slots=True)
class DailyWindow:
    recording_date: date
    start_at: float
    end_at: float

    @classmethod
    def from_date(cls, recording_date: date) -> DailyWindow:
        start = datetime.combine(recording_date, time(), SEOUL)
        return cls(recording_date, start.timestamp(), (start + timedelta(days=1)).timestamp())


def seoul_date(timestamp: float) -> date:
    return datetime.fromtimestamp(timestamp, SEOUL).date()


@dataclass(frozen=True, slots=True)
class StoredFrame:
    path: str
    received_at: float
    size_bytes: int
