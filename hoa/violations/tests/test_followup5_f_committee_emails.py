"""Committee notification emails (built after the owner's "go", 2026-11-25): the Compliance Committee members on the year's roster are emailed when a
meeting is set (one email for a whole repeating series), moved or cancelled, and once more a week before a meeting that has cases on its agenda.
The emails carry dates, places, links and COUNTS only: never an owner's name, address or any detail of a case. They can be switched off with
EMAIL_TO_COMMITTEE=0. Nothing leaves the machine: Django's in-memory outbox."""
from datetime import date, timedelta

import pytest
from django.core import mail
from django.core.management import call_command
from django.utils import timezone

from violations import committee_mail, services
from violations.models import CommitteeMeeting, CommitteeMember, Owner, Profile, Violation
from violations.tests.review4_support import future_meeting, make_case

S = Violation.Status
TODAY = date.today()
pytestmark = pytest.mark.django_db


@pytest.fixture
def roster(django_user_model):
    """Three members on this year's roster (a login with an email, an owner with an email, and one with neither), a duplicate, a member who left and last year's."""
    year = timezone.localtime(timezone.now() + timedelta(days=30)).year
    ann = django_user_model.objects.create_user("ann", "ann@example.com", "pw")
    Profile.objects.create(user=ann, role="committee")
    cy_home = Owner.objects.create(name="Cy Member", property_address="5 Member Way", email="cy@example.com")
    CommitteeMember.objects.create(name="Ann Chair", role="chair", year=year, user=ann)
    CommitteeMember.objects.create(name="Cy Member", year=year, owner=cy_home)
    CommitteeMember.objects.create(name="No Email", year=year)
    CommitteeMember.objects.create(name="Ann Again", year=year, owner=Owner.objects.create(name="Ann Dup", property_address="6 Dup Way", email="ANN@example.com"))
    CommitteeMember.objects.create(name="Left Early", year=year, active=False, owner=Owner.objects.create(name="L", property_address="7 L Way", email="left@example.com"))
    CommitteeMember.objects.create(name="Old Roster", year=year - 1, owner=Owner.objects.create(name="O", property_address="8 O Way", email="old@example.com"))
    return year


def sent_to():
    return sorted(a.lower() for m in mail.outbox for a in m.to)


def later(days=30, hour=18):
    t = timezone.localtime(timezone.now() + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return t


# ---- who gets them ----
def test_F5_only_this_years_active_members_with_an_email_and_each_address_once(roster):
    m = future_meeting()
    assert sorted(e.lower() for _n, e in committee_mail.recipients(m)) == ["ann@example.com", "cy@example.com"]


def test_F5_a_meeting_with_no_roster_emails_nobody_and_does_not_fail(django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        services.create_meeting(later(), zoom_link="https://z.example/1")
    assert mail.outbox == []


# ---- a meeting is set / moved / cancelled ----
def test_F5_a_new_meeting_is_emailed_to_each_member_with_the_date_the_place_and_a_link(roster, django_capture_on_commit_callbacks):
    t = later(40, 18)
    with django_capture_on_commit_callbacks(execute=True):
        m = services.create_meeting(t, zoom_link="https://z.example/abc", zoom_meeting_id="123 456", zoom_passcode="pw9")
    assert sent_to() == ["ann@example.com", "cy@example.com"] and all(len(x.to) == 1 for x in mail.outbox)      # each person gets their own copy
    b = mail.outbox[0].body
    assert "Dear Ann" in b and f"{t:%A, %B} {t.day}" in b and "6:00 PM" in b and "https://z.example/abc" in b and "123 456" in b
    assert f"/committee/meetings/{m.pk}/" in b and "Compliance Committee" in mail.outbox[0].subject


def test_F5_a_whole_series_is_one_email_listing_every_date(roster, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        made, err = services.create_meeting_series(later(40), 3, zoom_link="https://z.example/s")
    assert err == "" and len(made) == 3
    assert sent_to() == ["ann@example.com", "cy@example.com"]                       # not six
    assert all(f"{timezone.localtime(x.starts_at):%B} {timezone.localtime(x.starts_at).day}" in mail.outbox[0].body for x in made)


def test_F5_a_moved_meeting_says_from_when_to_when_even_if_the_owners_are_not_told(roster, django_capture_on_commit_callbacks):
    m = future_meeting(40)
    old = timezone.localtime(m.starts_at)
    new = later(50, 17)
    with django_capture_on_commit_callbacks(execute=True):
        services.reschedule_meeting(m, new, notify=False)
    assert sent_to() == ["ann@example.com", "cy@example.com"]
    b = mail.outbox[0].body
    assert "moved" in mail.outbox[0].subject.lower() and f"{old:%B} {old.day}" in b and f"{new:%B} {new.day}" in b and "5:00 PM" in b


def test_F5_a_cancelled_meeting_is_emailed_and_the_meeting_can_be_gone(roster, django_capture_on_commit_callbacks):
    m = future_meeting(40)
    when = timezone.localtime(m.starts_at)
    with django_capture_on_commit_callbacks(execute=True):
        services.cancel_meeting(m, notify=False)
    assert not CommitteeMeeting.objects.filter(pk=m.pk).exists() and sent_to() == ["ann@example.com", "cy@example.com"]
    assert "cancel" in mail.outbox[0].subject.lower() and f"{when:%B} {when.day}" in mail.outbox[0].body


def test_F5_a_meeting_change_that_is_undone_sends_nothing(roster, django_capture_on_commit_callbacks):
    m = future_meeting(40)
    old = m.starts_at
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(services.WorkflowError):
            services.reschedule_meeting(m, old)                    # the same time: refused
    assert mail.outbox == []


def test_F5_a_mail_problem_never_stops_the_meeting_from_being_saved(roster, monkeypatch, django_capture_on_commit_callbacks):
    def broken(message):
        raise OSError("mail server down")
    monkeypatch.setattr("violations.mailer.send", broken)
    with django_capture_on_commit_callbacks(execute=True):
        m = services.create_meeting(later(), zoom_link="https://z.example/1")
    assert CommitteeMeeting.objects.filter(pk=m.pk).exists()


# ---- the reminder a week before ----
def case_on(m, n):
    return make_case(status=S.HEARING_SCHEDULED, n=n, committee_meeting=m, hearing_date=m.starts_at)


def test_F5_a_week_before_a_meeting_with_cases_the_members_get_the_count_and_nothing_else(roster):
    m = future_meeting(7)
    a, b = case_on(m, 1), case_on(m, 2)
    Violation.objects.filter(pk=a.pk).update(description="Overgrown hedge behind the clubhouse")
    a.refresh_from_db()
    assert services.send_committee_reminders() == 1
    assert sent_to() == ["ann@example.com", "cy@example.com"]
    body = mail.outbox[0].body
    assert "2 cases" in body and f"/committee/meetings/{m.pk}/" in body and "reminder" in mail.outbox[0].subject.lower()
    for secret in (a.owner.name, a.owner.property_address, b.owner.property_address, a.description):
        assert secret not in body and secret not in mail.outbox[0].subject


def test_F5_the_reminder_goes_once_only(roster):
    m = future_meeting(7)
    case_on(m, 1)
    assert services.send_committee_reminders() == 1 and services.send_committee_reminders() == 0
    assert len(mail.outbox) == 2 and CommitteeMeeting.objects.get(pk=m.pk).committee_reminded_on == TODAY


def test_F5_a_missed_day_does_not_lose_the_reminder(roster):
    m = future_meeting(3)                            # the daily job did not run for four days
    case_on(m, 1)
    assert services.send_committee_reminders() == 1


@pytest.mark.parametrize("days,cases", [(7, 0), (8, 1), (30, 1)])
def test_F5_no_reminder_for_an_empty_agenda_or_a_meeting_still_far_off(roster, days, cases):
    m = future_meeting(days)
    for i in range(cases):
        case_on(m, i + 1)
    assert services.send_committee_reminders() == 0 and mail.outbox == []


def test_F5_no_reminder_for_a_meeting_that_has_happened(roster):
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() - timedelta(days=1), zoom_link="https://z.example/old")
    case_on(m, 1)
    assert services.send_committee_reminders() == 0


def test_F5_cases_already_decided_are_not_counted(roster):
    m = future_meeting(7)
    case_on(m, 1)
    make_case(status=S.LEDGER_PENDING, n=2, committee_meeting=m, hearing_date=m.starts_at)
    services.send_committee_reminders()
    assert "1 case is on the agenda" in mail.outbox[0].body and "2 cases" not in mail.outbox[0].body


def test_F5_the_reminder_waits_if_nobody_could_be_emailed(django_user_model):
    m = future_meeting(7)
    case_on(m, 1)
    assert services.send_committee_reminders() == 0 and CommitteeMeeting.objects.get(pk=m.pk).committee_reminded_on is None     # tried again once a roster exists


def test_F5_the_daily_job_runs_the_reminders(roster):
    m = future_meeting(7)
    case_on(m, 1)
    from io import StringIO
    out = StringIO()
    call_command("daily_jobs", stdout=out)
    assert "committee reminder" in out.getvalue().lower() and len(mail.outbox) >= 2


# ---- switches ----
def test_F5_everything_can_be_switched_off(roster, settings, django_capture_on_commit_callbacks):
    settings.EMAIL_TO_COMMITTEE = False
    m = future_meeting(7)
    case_on(m, 1)
    with django_capture_on_commit_callbacks(execute=True):
        services.create_meeting(later(), zoom_link="https://z.example/1")
        services.reschedule_meeting(m, later(60), notify=False)
    assert services.send_committee_reminders() == 0 and mail.outbox == []


def test_F5_the_reminder_alone_can_be_switched_off(roster, settings):
    settings.COMMITTEE_REMINDER_DAYS = 0
    m = future_meeting(7)
    case_on(m, 1)
    assert services.send_committee_reminders() == 0
