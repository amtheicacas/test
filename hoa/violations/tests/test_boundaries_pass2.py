"""Dates, money and counts exactly AT the rules' edges (second stabilisation pass, Nov 2026): hearing notice 13/14/20 days, midnight and daylight-saving
changes, cure deadline today/yesterday, the $1,000 cap, tied votes, weekends, month ends, the leap day. A rule that is off by one day or one dollar
here is the kind of mistake that makes a legal notice or a fine wrong."""
from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from violations import mailing, services
from violations.models import CommitteeMeeting, Violation
from violations.tests.test_extensions import case
from violations.tests.test_tenant_mail import rented  # noqa: F401

pytestmark = pytest.mark.django_db
S = Violation.Status
ET = ZoneInfo("America/New_York")
TODAY = date(2026, 12, 1)             # a Tuesday


def _meeting_at(local_day, hour=18, minute=0):
    return CommitteeMeeting.objects.create(starts_at=datetime(local_day.year, local_day.month, local_day.day, hour, minute, tzinfo=ET), zoom_link="https://z.example/1")


def _assign(v, meeting, today=TODAY):
    Violation.objects.filter(pk=v.pk).update(status=S.HEARING_NEEDED, committee_meeting=None)
    v.refresh_from_db()
    with mock.patch.object(mailing, "lob_send", return_value={"id": "ltr_1"}):
        return services.assign_to_meeting(v, meeting, today=today)


# ---- the 14-day minimum notice of a hearing (and the 20-day recommendation) --------------------------------------------------------------------
@pytest.mark.parametrize("days,ok", [(1, False), (13, False), (14, True), (15, True), (20, True), (60, True)])
def test_a_hearing_needs_at_least_14_days_notice(rented, days, ok):
    meeting = _meeting_at(TODAY + timedelta(days=days))
    if ok:
        _assign(rented, meeting)
        rented.refresh_from_db()
        assert rented.status == S.HEARING_SCHEDULED and rented.committee_meeting_id == meeting.pk
    else:
        with pytest.raises(services.WorkflowError, match="at least 14 days"):
            _assign(rented, meeting)
        rented.refresh_from_db()
        assert rented.status == S.HEARING_NEEDED


@pytest.mark.parametrize("hour,minute", [(0, 1), (9, 30), (23, 59)])
def test_the_14_day_rule_counts_the_local_date_whatever_the_time_of_day(rented, hour, minute):
    """A hearing at 11:59 PM Eastern is 04:59 UTC the next day: the rule must use the Eastern date, not the UTC one."""
    _assign(rented, _meeting_at(TODAY + timedelta(days=14), hour, minute))
    rented.refresh_from_db()
    assert rented.status == S.HEARING_SCHEDULED
    with pytest.raises(services.WorkflowError):
        _assign(rented, _meeting_at(TODAY + timedelta(days=13), 23, 59))


@pytest.mark.parametrize("day", [date(2027, 3, 14), date(2027, 3, 15), date(2026, 11, 1), date(2026, 11, 2), date(2028, 2, 29), date(2027, 1, 1)])
def test_hearing_dates_around_clock_changes_month_ends_and_the_leap_day_keep_their_local_date(rented, day):
    meeting = _meeting_at(day, 19, 0)
    assert timezone.localtime(meeting.starts_at).date() == day
    _assign(rented, meeting, today=day - timedelta(days=14))                          # exactly 14 days: allowed
    with pytest.raises(services.WorkflowError):
        _assign(rented, meeting, today=day - timedelta(days=13))                      # 13 days: refused


@pytest.mark.parametrize("days,short", [(13, True), (19, True), (20, False), (21, False)])
def test_the_under_20_days_warning_flips_exactly_at_20(days, short):
    m = CommitteeMeeting(starts_at=datetime.combine(TODAY + timedelta(days=days), datetime.min.time().replace(hour=18), tzinfo=ET))
    assert services.short_notice(m, today=TODAY) is short


# ---- the cure deadline: today is still in time, yesterday is not --------------------------------------------------------------------------------
def test_a_case_is_not_referred_on_the_deadline_day_but_is_the_day_after():
    v = case(deadline_in=0)
    assert services.advance_expired(today=date.today()) == 0 and Violation.objects.get(pk=v.pk).status == S.NOTICE_SENT
    assert services.advance_expired(today=date.today() + timedelta(days=1)) == 1 and Violation.objects.get(pk=v.pk).status == S.PENDING_BOARD


def test_a_deadline_extended_to_today_is_refused_but_tomorrow_is_fine():
    v = case(deadline_in=-30, status=S.PENDING_BOARD)
    with pytest.raises(services.WorkflowError, match="past"):
        services.grant_extension(v, new_deadline=date.today())
    services.grant_extension(v, new_deadline=date.today() + timedelta(days=1))
    assert Violation.objects.get(pk=v.pk).status == S.NOTICE_SENT


# ---- fines: $0, $100, $1,000 and over the cap ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("amount,problem", [(0, False), (100, False), (1000, False), ("1000.00", False), ("1000.01", True), (1001, True), (-1, True)])
def test_fine_amounts_at_the_edges_of_the_1000_dollar_cap(amount, problem):
    v = case()
    assert bool(services.fine_problems(v, amount)) is problem


def test_earlier_approved_fines_of_the_same_kind_count_towards_the_cap():
    v = case()
    other = case(owner={})
    Violation.objects.filter(pk=other.pk).update(owner=v.owner, vtype=v.vtype, status=S.FINE_APPROVED, fine_amount=950)
    assert services.fine_problems(v, 50) == [] and services.fine_problems(v, 50.01)
    Violation.objects.filter(pk=other.pk).update(status=S.REFERRED_LEGAL)      # NOTE-1: only 'fine approved' ones count today (reported to the Board)
    assert services.fine_problems(v, 1000) == []


# ---- votes: ties and the 3-member minimum ------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("outcome,votes,ok", [("confirm", (2, 2), False), ("confirm", (3, 0), True), ("confirm", (2, 1), True), ("confirm", (1, 1), False),
                                              ("reject", (2, 2), True), ("reject", (1, 2), True), ("reject", (3, 0), False), ("amend", (2, 2), False)])
def test_a_tied_vote_never_approves_a_fine_and_fewer_than_three_votes_never_count(django_user_model, outcome, votes, ok):
    from violations.tests.test_history_nav import _meeting_with
    m, (a,), cu = _meeting_with(1, django_user_model)
    kw = dict(amount="50") if outcome == "amend" else {}
    if ok:
        services.committee_record(a, outcome, cu, votes_for=votes[0], votes_against=votes[1], findings="x", **kw)
    else:
        with pytest.raises(services.WorkflowError):
            services.committee_record(a, outcome, cu, votes_for=votes[0], votes_against=votes[1], findings="x", **kw)


# ---- weekdays, month ends, year ends, the leap day -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("start,n,expected", [
    (date(2026, 11, 20), 1, date(2026, 11, 23)),          # Friday + 1 business day = Monday
    (date(2026, 11, 21), 1, date(2026, 11, 23)),          # Saturday + 1 = Monday
    (date(2026, 11, 22), 5, date(2026, 11, 27)),          # Sunday + 5 = Friday
    (date(2026, 12, 31), 1, date(2027, 1, 1)),            # across the year end (holidays are not skipped)
    (date(2028, 2, 28), 2, date(2028, 3, 1)),             # leap year: Mon Feb 28 -> Tue Feb 29 -> Wed Mar 1
    (date(2027, 2, 26), 1, date(2027, 3, 1)),             # not a leap year: Fri Feb 26 -> Mon Mar 1
    (date(2026, 11, 5), 0, date(2026, 11, 5))])
def test_business_days_across_weekends_month_ends_the_year_end_and_the_leap_day(start, n, expected):
    assert services.add_business_days(start, n) == expected


def test_a_monthly_meeting_series_keeps_its_weekday_across_the_year_end_and_a_fifth_weekday():
    first = datetime(2026, 11, 30, 18, 0, tzinfo=ET)                      # the 5th Monday of November
    label, dates = services.series_dates(first, 4)
    assert label == "last Monday of each month"
    assert [timezone.localtime(d).date() for d in dates] == [date(2026, 11, 30), date(2026, 12, 28), date(2027, 1, 25), date(2027, 2, 22)]
    assert all(timezone.localtime(d).hour == 18 for d in dates)           # still 6 PM local across the November clock change and in March
    _, spring = services.series_dates(datetime(2027, 2, 8, 18, 0, tzinfo=ET), 3)                              # 2nd Monday, over the March clock change
    assert [timezone.localtime(d).date() for d in spring] == [date(2027, 2, 8), date(2027, 3, 8), date(2027, 4, 12)]
    assert all(timezone.localtime(d).hour == 18 for d in spring)
