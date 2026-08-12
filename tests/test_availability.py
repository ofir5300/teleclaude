"""Tests for the pure parsing helpers in AvailabilityMixin."""

from datetime import datetime, timedelta

import pytest

from teleclaude._availability import AvailabilityMixin


@pytest.fixture
def mixin():
    return AvailabilityMixin()


@pytest.mark.parametrize(
    "text,expected",
    [
        ("5-hour limit reached, resets 6:50pm (Asia/Jerusalem).", "6:50pm (Asia/Jerusalem)"),
        ("Weekly limit reached, resets May 13 6:50pm.", "May 13 6:50pm"),
        ("Limit reached · resets Wednesday 6:50pm | try later", "Wednesday 6:50pm"),
        ("resets 7pm,", "7pm"),
        ("no hint here", ""),
    ],
)
def test_parse_reset(mixin, text, expected):
    assert mixin._watcher_parse_reset(text) == expected


def test_seconds_until_reset_future_time_today(mixin):
    target = (datetime.now() + timedelta(hours=2)).replace(second=0, microsecond=0)
    hint = target.strftime("%I:%M%p").lstrip("0").lower()
    secs = mixin._watcher_seconds_until_reset(hint)
    assert secs is not None
    assert 0 < secs <= 2 * 3600 + 60


def test_seconds_until_reset_rolls_to_tomorrow(mixin):
    target = (datetime.now() - timedelta(hours=2)).replace(second=0, microsecond=0)
    hint = target.strftime("%I:%M%p").lstrip("0").lower()
    secs = mixin._watcher_seconds_until_reset(hint)
    assert secs is not None
    assert secs > 20 * 3600


def test_seconds_until_reset_weekly_form_still_parses(mixin):
    # A weekday prefix must not defeat the parse (regression: re.match anchored
    # at the start returned None, forcing 15min fallback polling).
    assert mixin._watcher_seconds_until_reset("Wednesday 6:50pm") is not None


@pytest.mark.parametrize("hint", ["", "soon", "later today", "13:50", "25pm"])
def test_seconds_until_reset_unparseable(mixin, hint):
    assert mixin._watcher_seconds_until_reset(hint) is None


def test_seconds_until_reset_honours_timezone(mixin):
    # A named tz must be accepted (not crash) and yield a positive delta.
    secs = mixin._watcher_seconds_until_reset("6:50pm (Asia/Jerusalem)")
    assert secs is not None and secs > 0
