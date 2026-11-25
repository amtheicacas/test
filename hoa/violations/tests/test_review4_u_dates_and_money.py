"""Review pass 4 (R4-21): dates and money at their edges: the 14-day hearing rule (Florida time, late evening, midnight), monthly meeting series across the daylight-saving changes and the
5th weekday of a month, business days over weekends and year end, the fine cap to the cent, and typed amounts. (Guards: the code was already right; these keep it so.) Nothing is sent anywhere."""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from violations import services
from violations.models import Violation
from violations.tests.review4_support import future_meeting, make_case

pytestmark = pytest.mark.django_db
S = Violation.Status


def local(y, m, d, hh=0, mm=0):
    return timezone.make_aware(datetime(y, m, d, hh, mm))              # Florida (Eastern) time


@pytest.mark.parametrize("meeting_at, allowed", [
    (local(2026, 12, 7, 23, 59), True),          # exactly 14 days ahead, last minute of the day
    (local(2026, 12, 7, 0, 1), True),            # exactly 14 days ahead, first minute of the day
    (local(2026, 12, 6, 23, 59), False),         # 13 days ahead
    (local(2026, 12, 8, 0, 1), True),
])
def test_R4_21_the_fourteen_day_rule_goes_by_the_Florida_calendar_day_of_the_meeting(meeting_at, allowed):
    v = make_case(S.HEARING_NEEDED, proposed_fine=100, board_meeting_date=date(2026, 11, 23))
    m = future_meeting(30)
    m.starts_at = meeting_at
    m.save()
    if allowed:
        services.assign_to_meeting(v, m, None, today=date(2026, 11, 23))
        assert v.status == S.HEARING_SCHEDULED
    else:
        with pytest.raises(services.WorkflowError, match="at least 14 days"):
            services.assign_to_meeting(v, m, None, today=date(2026, 11, 23))


def test_R4_21_a_monthly_meeting_keeps_its_clock_time_across_both_daylight_saving_changes():
    label, dates = services.series_dates(local(2026, 10, 19, 18, 30), 8)             # 3rd Monday, 6:30 PM
    assert label == "3rd Monday of each month"
    assert [timezone.localtime(d).strftime("%Y-%m-%d %H:%M") for d in dates] == [
        "2026-10-19 18:30", "2026-11-16 18:30", "2026-12-21 18:30", "2027-01-18 18:30", "2027-02-15 18:30", "2027-03-15 18:30", "2027-04-19 18:30", "2027-05-17 18:30"]
    assert dates[0].utcoffset() != dates[1].utcoffset() and dates[4].utcoffset() != dates[5].utcoffset()          # (so the UTC time really does move)


def test_R4_21_the_fifth_weekday_of_a_month_means_the_last_one_each_month_and_the_year_rolls_over():
    label, dates = services.series_dates(local(2026, 11, 30, 19, 0), 4)              # the 5th Monday of November 2026
    assert label == "last Monday of each month"
    assert [timezone.localtime(d).date().isoformat() for d in dates] == ["2026-11-30", "2026-12-28", "2027-01-25", "2027-02-22"]


@pytest.mark.parametrize("start, days, expected", [
    (date(2026, 12, 31), 2, date(2027, 1, 4)),          # Thursday, over the weekend and the new year
    (date(2026, 11, 27), 2, date(2026, 12, 1)),         # Friday
    (date(2026, 11, 28), 2, date(2026, 12, 1)),         # Saturday
    (date(2026, 2, 27), 2, date(2026, 3, 3)),           # end of February
    (date(2028, 2, 28), 2, date(2028, 3, 1)),           # a leap year
])
def test_R4_21_business_days_skip_weekends_and_cross_month_and_year_ends(start, days, expected):
    assert services.add_business_days(start, days) == expected


@pytest.mark.parametrize("amount, ok", [("100", True), ("100.00", True), ("0.01", True), ("999999.99", True), ("1000000", False), ("-1", True), ("1e3", True),
                                        ("100.005", False), ("NaN", False), ("Infinity", False), ("", True), ("abc", False), ("1,000", False)])
def test_R4_21_a_typed_amount_is_a_plain_number_of_dollars_or_a_plain_message(amount, ok):
    if ok:
        services._amount(amount)
    else:
        with pytest.raises(services.WorkflowError):
            services._amount(amount)


def test_R4_21_the_fine_cap_is_checked_to_the_cent():
    v = make_case(S.PENDING_BOARD)
    earlier = Violation.objects.create(owner=v.owner, vtype=v.vtype, description="d", cure_required="c", status=S.FINE_APPROVED, fine_amount=Decimal("900.00"))
    assert services.fine_problems(v, Decimal("100.00")) == []                    # $1,000.00 in all: allowed
    assert services.fine_problems(v, Decimal("100.01"))                          # $1,000.01: refused
    assert services.fine_problems(v, Decimal("-5"))                              # never negative
    other_type = Violation.objects.create(owner=v.owner, vtype=type(v.vtype).objects.create(name="Other"), description="d", cure_required="c",
                                          status=S.FINE_APPROVED, fine_amount=Decimal("5000"))
    assert other_type.pk and services.fine_problems(v, Decimal("100.00")) == []     # (a different kind of violation does not count towards the cap)
    assert earlier.pk
