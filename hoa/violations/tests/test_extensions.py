import io
from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from PIL import Image

from violations import services
from violations.models import Notice, Owner, Photo, Profile, Reminder, Response, Violation, ViolationType

S = Violation.Status
pytestmark = pytest.mark.django_db
U = get_user_model()
TODAY = date.today()


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.REMINDER_DAYS = (7, 2)


def png():
    b = io.BytesIO()
    Image.new("RGB", (30, 20)).save(b, "PNG")
    return SimpleUploadedFile("a.png", b.getvalue(), content_type="image/png")


def case(deadline_in=5, status=S.NOTICE_SENT, email="p@example.com", **kw):
    o = Owner.objects.create(name="Pat", property_address=f"{Owner.objects.count() + 1} A St", email=email, **kw.pop("owner", {}))
    t = ViolationType.objects.get_or_create(name="Shed", defaults={"default_cure_days": 15})[0]
    v = Violation.objects.create(owner=o, vtype=t, description="d", cure_required="Remove the shed.", cure_days=15, status=status,
                                 cure_deadline=TODAY + timedelta(days=deadline_in), **kw)
    Photo.objects.create(violation=v, image=png())
    return v


def ask(v, days=10, text="Need more time, contractor is booked."):
    return services.add_response(v, text, from_resident=True, extension_requested=True, extension_days=days)


def login(client, role="manager"):
    u = U.objects.create_user(f"{role}{U.objects.count()}", password="pw-for-tests-123")
    Profile.objects.create(user=u, role=role)
    client.force_login(u)
    return u


# ---------- days are added to the cure time ----------
def test_days_are_added_to_the_current_deadline_not_to_today():
    v = case(deadline_in=3)
    services.grant_extension(v, days=10)
    v.refresh_from_db()
    assert v.cure_deadline == TODAY + timedelta(days=13) and v.extended_days == 10 and v.total_cure_days == 25
    services.grant_extension(v, days=5)
    v.refresh_from_db()
    assert v.cure_deadline == TODAY + timedelta(days=18) and v.extended_days == 15


def test_extension_that_would_still_be_in_the_past_is_refused_with_an_explanation():
    v = case(deadline_in=-20, status=S.PENDING_BOARD)
    with pytest.raises(services.WorkflowError, match="already be in the past"):
        services.grant_extension(v, days=10)
    services.grant_extension(v, new_deadline=TODAY + timedelta(days=7))  # an exact date still works
    v.refresh_from_db()
    assert v.status == S.NOTICE_SENT


def test_case_ages_against_the_extended_deadline():
    v = case(deadline_in=1)
    services.grant_extension(v, days=10)
    assert services.advance_expired(today=TODAY + timedelta(days=5)) == 0  # old deadline passed, still in time
    assert services.advance_expired(today=TODAY + timedelta(days=12)) == 1  # past the extended deadline: to the board
    v.refresh_from_db()
    assert v.status == S.PENDING_BOARD


def test_extension_notice_goes_out_with_the_new_date():
    v = case(deadline_in=2)
    services.grant_extension(v, days=7)
    n = v.notices.get(kind=Notice.Kind.EXTENSION)
    assert n.emailed_at and len(mail.outbox) == 1
    assert v.cure_deadline == TODAY + timedelta(days=9) and v.events.filter(text__contains="extended by 7 days").exists()


# ---------- approve / edit / decline ----------
def test_approve_requested_days(client):
    login(client)
    v = case(deadline_in=4)
    r = ask(v, 10)
    page = client.get(f"/violations/{v.pk}/").content.decode()
    assert "Owner asked for more time: 10 days" in page and f"{(TODAY + timedelta(days=14)):%b %-d, %Y}" in page
    client.post(f"/violations/{v.pk}/ext_approve/", {"response_id": r.pk, "days": 10})
    v.refresh_from_db()
    r.refresh_from_db()
    assert v.cure_deadline == TODAY + timedelta(days=14) and r.decision == "approved" and r.handled
    assert "Owner asked for more time" not in client.get(f"/violations/{v.pk}/").content.decode()


def test_approve_with_edited_days_or_exact_date(client):
    login(client)
    v = case(deadline_in=4)
    r = ask(v, 30)
    client.post(f"/violations/{v.pk}/ext_approve/", {"response_id": r.pk, "days": 5, "note": "Approved for 5 only"})
    v.refresh_from_db()
    assert v.cure_deadline == TODAY + timedelta(days=9) and v.extended_days == 5
    v2 = case(deadline_in=4)
    r2 = ask(v2, 30)
    exact = TODAY + timedelta(days=20)
    client.post(f"/violations/{v2.pk}/ext_approve/", {"response_id": r2.pk, "new_deadline": exact.isoformat()})
    v2.refresh_from_db()
    assert v2.cure_deadline == exact and v2.extended_days == 16


def test_approve_requires_days_or_date_and_cannot_be_answered_twice(client):
    login(client)
    v = case(deadline_in=4)
    r = ask(v, 10)
    client.post(f"/violations/{v.pk}/ext_approve/", {"response_id": r.pk})
    v.refresh_from_db()
    assert v.cure_deadline == TODAY + timedelta(days=4)
    client.post(f"/violations/{v.pk}/ext_approve/", {"response_id": r.pk, "days": 10})
    client.post(f"/violations/{v.pk}/ext_approve/", {"response_id": r.pk, "days": 10})  # second click: no double extension
    v.refresh_from_db()
    assert v.cure_deadline == TODAY + timedelta(days=14)


def test_decline_tells_the_owner_and_keeps_the_deadline(client):
    login(client)
    v = case(deadline_in=4)
    r = ask(v, 10)
    mail.outbox.clear()
    client.post(f"/violations/{v.pk}/ext_decline/", {"response_id": r.pk, "reason": "Work should take a day."})
    v.refresh_from_db()
    r.refresh_from_db()
    assert v.cure_deadline == TODAY + timedelta(days=4) and r.decision == "declined" and r.handled
    reply = v.responses.filter(from_resident=False).get()
    assert "not approved" in reply.message and "Work should take a day." in reply.message
    assert len(mail.outbox) == 1 and "Work should take a day." in mail.outbox[0].body


def test_board_queue_shows_the_request_with_approve_and_decline(client):
    login(client, "board")
    v = case(deadline_in=-3, status=S.PENDING_BOARD)
    r = ask(v, 10)
    page = client.get("/board/").content.decode()
    assert "Owner asked for more time: 10 days" in page and "Approve" in page and "Decline" in page
    client.post(f"/violations/{v.pk}/ext_approve/", {"response_id": r.pk, "days": 10, "next": "/board/"})
    v.refresh_from_db()
    assert v.status == S.NOTICE_SENT and v.cure_deadline == TODAY + timedelta(days=7)  # old deadline + 10 days


def test_board_vote_can_grant_days(client):
    login(client, "board")
    v = case(deadline_in=-3, status=S.PENDING_BOARD)
    client.post(f"/violations/{v.pk}/board/", {"decision": "extend", "meeting_date": TODAY.isoformat(), "extension_days": 10})
    v.refresh_from_db()
    assert v.status == S.NOTICE_SENT and v.cure_deadline == TODAY + timedelta(days=7)


def test_staff_initiated_extension_by_days(client):
    login(client)
    v = case(deadline_in=4)
    client.post(f"/violations/{v.pk}/extension/", {"days": 6})
    v.refresh_from_db()
    assert v.cure_deadline == TODAY + timedelta(days=10)


def test_owner_must_say_how_many_days(client):
    v = case(deadline_in=4)
    r = client.post(f"/r/{v.token}/", {"message": "More time please", "request_extension": "on"})
    assert r.status_code == 200 and b"how many more days" in r.content.lower() and v.responses.count() == 0
    r = client.post(f"/r/{v.token}/", {"message": "More time please", "request_extension": "on", "extension_days": 7})
    assert r.status_code == 302 and v.responses.get().extension_days == 7


# ---------- reminders ----------
def test_reminders_at_seven_and_two_days_and_never_twice():
    v = case(deadline_in=15)
    sent = lambda d: services.send_cure_reminders(today=TODAY + timedelta(days=15 - d))  # d = days left
    assert [sent(d) for d in (15, 10, 8)] == [0, 0, 0]
    assert sent(7) == 1
    assert sent(7) == 0 and sent(6) == 0 and sent(3) == 0  # same threshold: not again
    assert sent(2) == 1 and sent(2) == 0 and sent(1) == 0
    assert services.send_cure_reminders(today=TODAY + timedelta(days=16)) == 0  # after the deadline
    assert len(mail.outbox) == 2
    assert "7 days" in mail.outbox[0].subject and "2 days" in mail.outbox[1].subject
    body = mail.outbox[0].body
    assert "Remove the shed." in body and v.resident_url() in body and f"{v.cure_deadline:%B %d, %Y}" in body
    assert v.events.filter(text__startswith="Reminder emailed").count() == 2


def test_missed_days_send_only_the_most_urgent_reminder_once():
    case(deadline_in=1)
    assert services.send_cure_reminders() == 1
    assert services.send_cure_reminders() == 0
    assert len(mail.outbox) == 1 and "tomorrow" in mail.outbox[0].subject
    assert Reminder.objects.filter(sent_at__isnull=False).count() == 1 and Reminder.objects.count() == 2


def test_extension_restarts_the_reminders():
    v = case(deadline_in=2)
    assert services.send_cure_reminders() == 1
    services.grant_extension(v, days=20)
    mail.outbox.clear()
    assert services.send_cure_reminders() == 0  # 22 days left
    assert services.send_cure_reminders(today=TODAY + timedelta(days=15)) == 1  # 7 left on the new deadline
    assert "7 days" in mail.outbox[0].subject


def test_no_reminders_when_on_hold_pending_board_no_email_or_owner_says_fixed():
    case(deadline_in=3, status=S.ON_HOLD)
    case(deadline_in=3, status=S.PENDING_BOARD)
    case(deadline_in=3, email="")
    fixed = case(deadline_in=3)
    services.add_response(fixed, "All fixed!", from_resident=True, cure_reported=True)
    assert services.send_cure_reminders() == 0 and mail.outbox == []


def test_tenant_is_included():
    case(deadline_in=3, owner={"tenant_name": "Tina", "tenant_email": "tina@example.com"})
    services.send_cure_reminders()
    assert set(mail.outbox[0].to) == {"p@example.com", "tina@example.com"}


def test_reminder_days_are_configurable(settings):
    settings.REMINDER_DAYS = (5,)
    case(deadline_in=6)
    assert services.send_cure_reminders() == 0
    assert services.send_cure_reminders(today=TODAY + timedelta(days=1)) == 1


def test_daily_jobs_command_sends_reminders_and_refers_expired():
    a = case(deadline_in=2)
    b = case(deadline_in=-1)
    call_command("daily_jobs")
    a.refresh_from_db()
    b.refresh_from_db()
    assert b.status == S.PENDING_BOARD and a.status == S.NOTICE_SENT
    assert len(mail.outbox) == 1
    call_command("send_reminders")
    assert len(mail.outbox) == 1  # nothing new
