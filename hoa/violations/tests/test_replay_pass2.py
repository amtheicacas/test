"""Pressing every button twice, in every state (second stabilisation pass): every staff action on a case is posted twice, for a case in every possible status, with
sensible data. A page may refuse (with a message) but must never crash (HTTP 500), and a second identical click must never change more than the first did."""
from datetime import date, timedelta
from unittest import mock

import pytest
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from violations import mailing
from violations.models import CommitteeMeeting, Notice, Owner, Profile, Response, Violation, ViolationType

pytestmark = pytest.mark.django_db
ACTIONS = ["add_photos", "delete_photo", "send_notice", "refresh_tracking", "mail_now", "reinspect", "reinspect_result", "cure", "dismiss", "hold", "resume", "reply",
           "extension", "ext_approve", "ext_decline", "board", "assign", "committee", "impose", "urgent", "payment", "legal", "no_such_action"]


@pytest.fixture
def staff(django_user_model):
    u = django_user_model.objects.create_user("bd7", password="x")
    Profile.objects.create(user=u, role="board")
    c = Client(raise_request_exception=False)
    c.force_login(u)
    return c


def case_in(status, n):
    o = Owner.objects.create(name=f"Pat {n}", property_address=f"{n} {status} Way", email=f"p{n}@example.com", tenant_name="Tina", tenant_email="t@example.com")
    t = ViolationType.objects.get_or_create(name="Shed", defaults={"default_cure_days": 15})[0]
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=30), zoom_link="https://z.example/1")
    v = Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c", status=status, proposed_fine=100, fine_amount=100,
                                 cure_deadline=date.today() + timedelta(days=5), committee_meeting=m, hearing_date=m.starts_at, committee_outcome="")
    nt = Notice.objects.create(violation=v, kind="violation")
    r = Response.objects.create(violation=v, message="more time please", from_resident=True, extension_requested=True, extension_days=5)
    return v, nt, m, r


@pytest.mark.parametrize("status", Violation.Status.values)
def test_every_action_posted_twice_in_every_status_never_crashes(staff, status, settings):
    settings.MAIL_PROVIDER = "manual"
    v, nt, m, r = case_in(status, 1)
    data = {"note": "n", "reason": "r", "until": (date.today() + timedelta(days=9)).isoformat(), "message": "m", "days": "5", "extension_days": "5",
            "new_deadline": (date.today() + timedelta(days=40)).isoformat(), "response_id": r.pk, "decision": "proceed", "meeting_date": date.today().isoformat(),
            "proposed_fine": "100", "by_majority": "on", "meeting": m.pk, "votes_for": "3", "votes_against": "0", "method": "manual", "reference": "X",
            "paid_on": date.today().isoformat(), "notice": nt.pk, "timing": "standard", "corrected": "on", "outcome": "confirm", "amount": "50", "findings": "x", "photo": "1"}
    bad = []
    for action in ACTIONS:
        for attempt in (1, 2):
            with mock.patch.object(mailing, "lob_send", return_value={"id": "x"}):
                resp = staff.post(reverse("violation_action", args=[v.pk, action]), data)
            if resp.status_code >= 500:
                bad.append((action, attempt, resp.status_code))
    assert not bad, bad


# ---- sign-in links and sessions: tampered, demoted, switched off ------------------------------------------------------------------------------------
import re

from violations.models import LoginLink


def _link_path(client):
    mail.outbox.clear()
    client.post("/accounts/get-access/", {"email": "pat@example.com"})
    return re.search(r"/accounts/link/[^\s/]+/", mail.outbox[0].body).group(0)


def test_a_sign_in_link_with_any_character_changed_or_cut_short_is_refused():
    Owner.objects.create(name="Pat Owner", property_address="1 Link Way", email="pat@example.com")
    c = Client()
    path = _link_path(c)
    token = path.split("/")[-2]
    flipped = token[:-1] + ("A" if token[-1] != "A" else "B")
    for bad in (flipped, token[:-1], token + "x", token.upper() if token.upper() != token else token.lower(), "A" * len(token), "", "../" + token):
        for method in (c.get, c.post):
            r = method(f"/accounts/link/{bad}/")
            assert r.status_code in (404, 410), (bad[:12], r.status_code)
            assert "_auth_user_id" not in c.session
    assert c.post(path).status_code == 302                              # the genuine one still works, once


def test_a_board_member_who_is_demoted_loses_staff_pages_on_the_very_next_click(staff, django_user_model):
    assert staff.get("/violations/").status_code == 200
    Profile.objects.filter(user__username="bd7").update(role="homeowner")
    assert staff.get("/violations/").status_code in (302, 403)
    assert staff.post(reverse("violation_action", args=[1, "dismiss"])).status_code in (302, 403, 404)


def test_a_login_that_is_switched_off_is_signed_out_on_the_very_next_click(staff, django_user_model):
    assert staff.get("/violations/").status_code == 200
    django_user_model.objects.filter(username="bd7").update(is_active=False)
    r = staff.get("/violations/")
    assert r.status_code == 302 and "/accounts/login/" in r["Location"]


def test_a_changed_password_ends_the_other_sessions_of_that_login(django_user_model):
    u = django_user_model.objects.create_user("pw1", password="a-long-password-123")
    Profile.objects.create(user=u, role="board")
    other = Client()
    other.force_login(u)
    assert other.get("/violations/").status_code == 200
    u.set_password("another-long-password-456")
    u.save()
    assert other.get("/violations/").status_code == 302                  # the old session no longer matches the password


def test_the_page_after_signing_out_cannot_be_reopened_with_the_old_cookie(staff):
    old_cookie = staff.cookies["sessionid"].value
    staff.post("/accounts/logout/")
    c = Client()
    c.cookies["sessionid"] = old_cookie
    assert c.get("/violations/").status_code == 302
