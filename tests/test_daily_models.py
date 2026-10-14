import importlib
import importlib.util
from datetime import date

import pytest

from camera_media_service.recording import iso


def models():
    assert importlib.util.find_spec("camera_media_service.daily_models"), "Daily date model missing"
    return importlib.import_module("camera_media_service.daily_models")


def test_seoul_day_is_bounded_by_utc_1500():
    window = models().DailyWindow.from_date(date(2026, 10, 14))
    assert iso(window.start_at) == "2026-10-13T15:00:00.000Z"
    assert iso(window.end_at) == "2026-10-14T15:00:00.000Z"
    assert window.end_at - window.start_at == 86400
    assert models().seoul_date(window.end_at - 0.001) == date(2026, 10, 14)
    assert models().seoul_date(window.end_at) == date(2026, 10, 15)


@pytest.mark.parametrize(
    "value,end",
    [
        (date(2024, 2, 29), "2024-02-29T15:00:00.000Z"),
        (date(2026, 12, 31), "2026-12-31T15:00:00.000Z"),
    ],
)
def test_day_boundaries_across_leap_and_year_end(value, end):
    assert iso(models().DailyWindow.from_date(value).end_at) == end
