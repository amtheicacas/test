"""Security probes for the 2026-11-25 changes (second home, deadline limit, photo cap, Lob idempotency key, resend decision letter, committee emails,
missing stored file). Each test is an attack or an abuse of the new code, not a happy path. Nothing is sent anywhere (Django's outbox)."""
import threading
from datetime import date, timedelta

import pytest
from django.core import mail
from django.db import connection, connections
from django.test import Client
from django.utils import timezone

from violations import committee_mail, mailing, services
from violations.models import CommitteeMeeting, CommitteeMember, Notice, Owner, Profile, Violation
from violations.tests.review4_support import board, future_meeting, make_case  # noqa: F401  (board is a fixture)
from violations.tests.test_followup5_e_resend_decision_letter import imposed

S = Violation.Status
TODAY = date.today()
pytestmark = pytest.mark.django_db


def login(django_user_model, name, role, owner=None, csrf=False):
    u = django_user_model.objects.create_user(name, f"{name}@example.com", "pw")
    Profile.objects.create(user=u, role=role, owner=owner)
    c = Client(raise_request_exception=False, enforce_csrf_checks=csrf)
    c.force_login(u)
    return c


# ---- who may press "Send the decision letter again" ----
@pytest.mark.parametrize("who,allowed", [("anonymous", False), ("homeowner", False), ("committee", False), ("board", True), ("manager", True)])
def test_SEC5_only_the_board_and_the_manager_can_send_a_decision_letter_again(django_user_model, settings, who, allowed):
    settings.MAIL_PROVIDER = "manual"
    v = imposed()
    if who == "anonymous":
        c = Client(raise_request_exception=False)
    else:
        c = login(django_user_model, who, who, owner=v.owner if who == "homeowner" else None)
    r = c.post(f"/violations/{v.pk}/resend_decision_letter/")
    sent = Notice.objects.filter(violation=v, kind="fine").exists()
    assert sent is allowed, (who, r.status_code)
    assert len(mail.outbox) == (1 if allowed else 0)


def test_SEC5_the_button_needs_a_post_and_a_valid_csrf_token(django_user_model, settings):
    settings.MAIL_PROVIDER = "manual"
    v = imposed()
    c = login(django_user_model, "bd", "board", csrf=True)
    assert c.get(f"/violations/{v.pk}/resend_decision_letter/").status_code == 405          # a link someone is tricked into opening does nothing
    assert c.post(f"/violations/{v.pk}/resend_decision_letter/").status_code == 403         # a form on another site does nothing
    assert not Notice.objects.filter(violation=v, kind="fine").exists() and mail.outbox == []


def test_SEC5_a_case_number_that_does_not_exist_or_is_not_imposed_changes_nothing(board, settings):
    settings.MAIL_PROVIDER = "manual"
    assert board.post("/violations/999999/resend_decision_letter/").status_code == 404
    v = make_case(status=S.NOTICE_SENT)
    board.post(f"/violations/{v.pk}/resend_decision_letter/", follow=True)
    assert Notice.objects.count() == 0 and mail.outbox == []


# ---- hostile text in what the committee emails carry ----
EVIL = "Room 1\r\nBcc: attacker@example.com\r\nSubject: hijacked"


def test_SEC5_a_line_break_in_the_place_or_a_member_name_cannot_add_a_recipient_or_a_header(django_user_model, django_capture_on_commit_callbacks):
    year = timezone.localtime(timezone.now() + timedelta(days=30)).year
    owner = Owner.objects.create(name="Mem Ber", property_address="5 Member Way", email="member@example.com")
    CommitteeMember.objects.create(name="Mem\r\nBcc: attacker@example.com", year=year, owner=owner)
    with django_capture_on_commit_callbacks(execute=True):
        services.create_meeting(timezone.now() + timedelta(days=30), zoom_link="https://z.example/1", location=EVIL)
    assert len(mail.outbox) == 1
    m = mail.outbox[0]
    assert m.to == ["member@example.com"] and m.bcc == [] and m.cc == [] and "attacker@example.com" not in str(m.message()["Bcc"] or "")
    assert "hijacked" not in m.subject and "\n" not in m.subject and m.message()["Subject"].count("hijacked") == 0


def test_SEC5_a_roster_address_with_a_line_break_is_skipped_and_nothing_else_breaks(django_capture_on_commit_callbacks):
    year = timezone.localtime(timezone.now() + timedelta(days=30)).year
    CommitteeMember.objects.create(name="Bad", year=year, owner=Owner.objects.create(name="B", property_address="1 B Way", email="ok@example.com\nBcc: x@example.com"))
    CommitteeMember.objects.create(name="Good", year=year, owner=Owner.objects.create(name="G", property_address="2 G Way", email="good@example.com"))
    with django_capture_on_commit_callbacks(execute=True):
        m = services.create_meeting(timezone.now() + timedelta(days=30), zoom_link="https://z.example/1")
    assert CommitteeMeeting.objects.filter(pk=m.pk).exists()
    assert [x.to for x in mail.outbox] == [["good@example.com"]]


def test_SEC5_the_reminder_never_names_an_owner_or_an_address_even_with_hostile_case_text():
    year = timezone.localtime(timezone.now() + timedelta(days=7)).year
    CommitteeMember.objects.create(name="Cmte", year=year, owner=Owner.objects.create(name="C", property_address="3 C Way", email="cmte@example.com"))
    m = future_meeting(7)
    v = make_case(status=S.HEARING_SCHEDULED, committee_meeting=m, hearing_date=m.starts_at)
    Owner.objects.filter(pk=v.owner_id).update(name="Zed <script>alert(1)</script> Zyx", property_address="77 Secret Lane")
    Violation.objects.filter(pk=v.pk).update(description="Hoarding of <b>private</b> things")
    assert services.send_committee_reminders() == 1
    text = mail.outbox[0].subject + mail.outbox[0].body
    for leak in ("Zed", "Zyx", "Secret Lane", "Hoarding", "private", "@example.com"):
        assert leak not in text, leak


# ---- the owner of a case cannot reach the new pages ----
def test_SEC5_an_owner_page_never_shows_the_missing_letter_box_or_the_committee_link(django_user_model, settings):
    settings.MAIL_PROVIDER = "manual"
    v = imposed()
    c = login(django_user_model, "own", "homeowner", owner=v.owner)
    for url in ("/me/violations/", f"/me/{v.pk}/", "/"):
        page = c.get(url, follow=True).content.decode()
        assert "decision letter again" not in page and "Decision letters not sent" not in page and "NOT been sent" not in page, url


def test_SEC5_hostile_owner_text_is_escaped_in_the_new_box_and_on_the_home_screen(board, settings):
    settings.MAIL_PROVIDER = "manual"
    v = imposed()
    Owner.objects.filter(pk=v.owner_id).update(property_address='9 <img src=x onerror=alert(1)> Way', name="<script>alert(2)</script>")
    for url in (f"/violations/{v.pk}/", "/workspace/"):
        page = board.get(url).content.decode()
        assert "<img src=x" not in page and "<script>alert(2)" not in page, url
        assert "&lt;img src=x" in page or "&lt;script&gt;alert(2)" in page


# ---- the second-home rule cannot be dodged by spelling, and does not over-reach ----
def test_SEC5_second_home_matches_any_capitals_and_spaces_but_not_a_similar_address_or_the_tenant(django_user_model):
    home = Owner.objects.create(name="H", property_address="4 H Way", email="  Bob@Example.COM  ", tenant_email="rita@example.com")
    v = make_case(status=S.NOTICE_SENT, n=40)
    Violation.objects.filter(pk=v.pk).update(owner=home)
    v.refresh_from_db()
    bob = django_user_model.objects.create_user("bobx", "bob@example.com", "pw")
    plus = django_user_model.objects.create_user("bobplus", "bob+hoa@example.com", "pw")
    rita = django_user_model.objects.create_user("ritax", "rita@example.com", "pw")
    other = django_user_model.objects.create_user("bob2", "bob@example.com.au", "pw")
    for u in (bob, plus, rita, other):
        Profile.objects.create(user=u, role="board")
    assert services.own_household(bob, v)
    assert not services.own_household(plus, v) and not services.own_household(other, v)
    assert not services.own_household(rita, v)          # a tenant's address is not an owner's


# ---- the idempotency key ----
def test_SEC5_the_key_reveals_nothing_about_the_case_and_cannot_carry_a_header_injection(rented_case=None):
    v = make_case()
    n = Notice.objects.create(violation=v, kind="hearing")
    k = mailing.letter_key(n, "owner\r\nX-Evil: 1", "certified", {"name": "A\r\nB", "line1": "1 X", "city": "C", "state": "FL", "zip": "34236"})
    assert k.startswith("hoa-") and len(k) == 44 and all(ch in "0123456789abcdef" for ch in k[4:])
    for secret in (v.token, v.owner.name, v.owner.property_address, "certified"):
        assert secret not in k


# ---- real two-at-once presses (PostgreSQL only) ----
def _at_once(*jobs):
    start, out = threading.Barrier(len(jobs)), [None] * len(jobs)

    def run(i, job):
        try:
            start.wait(timeout=10)
            out[i] = ("ok", job())
        except services.WorkflowError as e:
            out[i] = ("refused", str(e))
        except Exception as e:      # noqa: BLE001
            out[i] = ("crashed", f"{type(e).__name__}: {e}")
        finally:
            connections.close_all()
    ts = [threading.Thread(target=run, args=(i, j)) for i, j in enumerate(jobs)]
    [t.start() for t in ts]
    [t.join(timeout=60) for t in ts]
    return out


@pytest.mark.django_db(transaction=True)
def test_SEC5_two_people_pressing_send_the_decision_letter_again_at_once_send_one(settings):
    if connection.vendor != "postgresql":
        pytest.skip("row-level concurrency needs PostgreSQL")
    settings.MAIL_PROVIDER = "manual"
    v = imposed()
    results = _at_once(lambda: services.resend_decision_letter(Violation.objects.get(pk=v.pk), None),
                       lambda: services.resend_decision_letter(Violation.objects.get(pk=v.pk), None),
                       lambda: services.resend_decision_letter(Violation.objects.get(pk=v.pk), None))
    assert sorted(r[0] for r in results) == ["ok", "refused", "refused"], results
    assert Notice.objects.filter(violation=v, kind="fine").count() == 1 and len(mail.outbox) == 1


@pytest.mark.django_db(transaction=True)
def test_SEC5_two_daily_jobs_at_once_send_the_committee_reminder_once():
    if connection.vendor != "postgresql":
        pytest.skip("row-level concurrency needs PostgreSQL")
    year = timezone.localtime(timezone.now() + timedelta(days=7)).year
    CommitteeMember.objects.create(name="Cmte", year=year, owner=Owner.objects.create(name="C", property_address="3 C Way", email="cmte@example.com"))
    m = future_meeting(7)
    make_case(status=S.HEARING_SCHEDULED, committee_meeting=m, hearing_date=m.starts_at)
    results = _at_once(lambda: services.send_committee_reminders(), lambda: services.send_committee_reminders(), lambda: services.send_committee_reminders())
    assert sorted(r[1] for r in results) == [0, 0, 1], results
    assert len(mail.outbox) == 1
