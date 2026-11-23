"""Review pass 4 (R4-08): The 'too many tries' window slid forward on every refused try, so it never freed itself while someone kept trying.
Every test here FAILED on the code before its fix. Nothing is sent anywhere (Django's in-memory outbox; the print service is never called)."""
from datetime import date
from unittest import mock

import pytest

from violations import throttle
from violations.models import (Violation)
from violations.tests.review4_support import board, browser_form, future_meeting, make_case  # noqa: F401  (board is a fixture)

pytestmark = pytest.mark.django_db
S = Violation.Status
TODAY = date.today()


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


def test_R4_08_the_try_counter_frees_itself_an_hour_after_the_first_try_whatever_was_tried_in_between():
    clock = Clock()
    with mock.patch("time.time", clock):
        assert [throttle.hit("r4:x", limit=3, window=3600) for _ in range(4)] == [False, False, False, True]      # the 4th is refused
        clock.now += 50 * 60
        assert throttle.hit("r4:x", limit=3, window=3600) is True                                                    # still refused 50 minutes in
        clock.now += 20 * 60                                                                                        # 70 minutes after the first try
        assert throttle.hit("r4:x", limit=3, window=3600) is False                                                   # the hour is over: allowed again


def test_R4_08_the_counter_still_counts_within_the_hour():
    clock = Clock()
    with mock.patch("time.time", clock):
        assert [throttle.hit("r4:y", limit=2, window=3600) for _ in range(3)] == [False, False, True]
        clock.now += 59 * 60
        assert throttle.hit("r4:y", limit=2, window=3600) is True
