"""Stabilisation pass: tests for bugs found by reading the code. One test (or more) per fix."""
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from violations.models import CommitteeMeeting, CommitteeMember, Owner, Profile, Violation, ViolationType

pytestmark = pytest.mark.django_db
U = get_user_model()
S = Violation.Status


def mk(name, role, owner=None):
    u = U.objects.create_user(name, f"{name}@example.com", "x", first_name=name.title(), last_name="T")
    Profile.objects.create(user=u, role=role, owner=owner)
    return u


def past_hearing_case(owner):
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() - timedelta(hours=2), zoom_link="https://z.example/1")
    t = ViolationType.objects.get_or_create(name="Trash cans", defaults={"default_cure_days": 15})[0]
    v = Violation.objects.create(owner=owner, vtype=t, description="d", cure_required="c", status=S.HEARING_SCHEDULED, proposed_fine=100,
                                 committee_meeting=m, hearing_date=m.starts_at)
    return m, v


def test_a_committee_member_cannot_vote_on_their_own_violation_through_the_old_vote_page(client):
    """F-01: the old /committee/cases/<id>/vote/ page had no abstention check, so an owner who also sits on the committee could decide
    (reject) their own fine. The meeting page already blocks this; the old page must too."""
    me = Owner.objects.create(name="Dual Member", property_address="1 Own Way", email="dual@example.com")
    m, v = past_hearing_case(me)
    u = mk("dual", "committee", me)
    CommitteeMember.objects.create(name="Dual Member", year=timezone.localdate().year, user=u, owner=me)
    client.force_login(u)
    r = client.post(f"/committee/cases/{v.pk}/vote/", {"votes_for": 0, "votes_against": 3, "findings": "I reject my own fine."}, follow=True)
    v.refresh_from_db()
    assert v.status == S.HEARING_SCHEDULED and v.committee_votes_for is None
    assert "your own violation" in r.content.decode()


def test_a_household_member_on_the_roster_cannot_vote_either(client):
    owner = Owner.objects.create(name="Pat Owner", property_address="2 Own Way", email="pat@example.com")
    m, v = past_hearing_case(owner)
    spouse = mk("spouse", "committee")                                         # a different login, but the roster says it is the same household
    CommitteeMember.objects.create(name="Spouse Owner", year=timezone.localdate().year, user=spouse, owner=owner)
    client.force_login(spouse)
    client.post(f"/committee/cases/{v.pk}/vote/", {"votes_for": 0, "votes_against": 3, "findings": "x"})
    v.refresh_from_db()
    assert v.status == S.HEARING_SCHEDULED


def test_an_unrelated_committee_member_can_still_use_the_old_vote_page_as_before(client):
    """The old page keeps working for everyone else: this fix only stops the conflict of interest."""
    other = Owner.objects.create(name="Someone Else", property_address="3 Other Way", email="se@example.com")
    m, v = past_hearing_case(other)
    u = mk("member", "committee")
    CommitteeMember.objects.create(name="Member", year=timezone.localdate().year, user=u)
    client.force_login(u)
    client.post(f"/committee/cases/{v.pk}/vote/", {"votes_for": 3, "votes_against": 0, "findings": "Confirmed."})
    v.refresh_from_db()
    assert v.status == S.LEDGER_PENDING and v.committee_votes_for == 3


# ---------- F-02: the "next" field of staff forms could send a Board member to another website ----------
@pytest.mark.parametrize("evil", ["https://evil.example/x", "//evil.example/x", "javascript:alert(1)", "http://testserver.evil.example/", "\\\\evil.example"])
def test_a_crafted_next_address_is_ignored_after_a_board_form(client, evil):
    boss = mk("boss", "board")
    o = Owner.objects.create(name="Alex", property_address="9 Next Way", email="n@example.com")
    t = ViolationType.objects.get_or_create(name="Trash cans", defaults={"default_cure_days": 15})[0]
    v = Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c", status=S.NOTICE_SENT)
    client.force_login(boss)
    r = client.post(f"/violations/{v.pk}/board/", {"next": evil})
    assert r.status_code == 302 and r["Location"] == v.get_absolute_url()


def test_a_next_address_on_this_site_is_still_honoured(client):
    boss = mk("boss", "board")
    o = Owner.objects.create(name="Alex", property_address="9 Next Way", email="n@example.com")
    t = ViolationType.objects.get_or_create(name="Trash cans", defaults={"default_cure_days": 15})[0]
    v = Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c", status=S.NOTICE_SENT)
    client.force_login(boss)
    r = client.post(f"/violations/{v.pk}/board/", {"next": "/board/?page=2"})
    assert r["Location"] == "/board/?page=2"


# ---------- F-03: two people (or a double click) acting on the same case at once ----------
def test_a_stale_second_copy_of_a_case_cannot_issue_the_first_notice_twice(client):
    from django.core import mail
    from violations import services
    from violations.models import Notice
    o = Owner.objects.create(name="Alex", property_address="7 Race Way", email="race@example.com")
    t = ViolationType.objects.get_or_create(name="Trash cans", defaults={"default_cure_days": 15})[0]
    v = Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c", status=S.DRAFT)
    import io
    from django.core.files.base import ContentFile
    from PIL import Image
    from violations.models import Photo
    img = io.BytesIO(); Image.new("RGB", (20, 20), "red").save(img, "JPEG")
    ph = Photo(violation=v, from_resident=False); ph.image.save("p.jpg", ContentFile(img.getvalue()), save=True)
    first, second = Violation.objects.get(pk=v.pk), Violation.objects.get(pk=v.pk)           # two requests loaded the case while it was still a draft
    services.issue_first_notice(first, None)
    sent = len(mail.outbox)
    with pytest.raises(services.WorkflowError):
        services.issue_first_notice(second, None)
    assert len(mail.outbox) == sent and Notice.objects.filter(violation=v, kind="violation").count() == 1


# ---------- F-04: a failure while building a recipient's letter must be recorded against THAT recipient ----------
def _case_with_owner_and_tenant():
    import io
    from django.core.files.base import ContentFile
    from PIL import Image
    from violations.models import Photo
    o = Owner.objects.create(name="Alex Owner", property_address="5 Fail Way", email="owner@example.com", tenant_name="Tina Tenant",
                             tenant_email="tenant@example.com", tenant_start=timezone.localdate() - timedelta(days=30),
                             tenant_end=timezone.localdate() + timedelta(days=300))
    t = ViolationType.objects.get_or_create(name="Trash cans", defaults={"default_cure_days": 15})[0]
    v = Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c", status=S.NOTICE_SENT)
    img = io.BytesIO(); Image.new("RGB", (20, 20), "red").save(img, "JPEG")
    ph = Photo(violation=v, from_resident=False); ph.image.save("p.jpg", ContentFile(img.getvalue()), save=True)
    return v


def test_a_letter_that_cannot_be_built_is_recorded_as_a_failure_not_a_crash():
    from unittest import mock
    from violations import services
    from violations.models import EmailLog, Notice
    v = _case_with_owner_and_tenant()
    with mock.patch.object(services, "render_notice_pdf", side_effect=RuntimeError("cannot build the pdf")):
        n = services.send_notice(v, Notice.Kind.VIOLATION, None, hold_mail_days=2)            # must not raise
    assert "cannot build the pdf" in n.email_error and n.emailed_at is None
    logs = EmailLog.objects.filter(violation=v, ok=False)
    assert logs.count() >= 1 and all("Notice of violation" in e.subject.capitalize() or "violation" in e.subject.lower() for e in logs)


def test_when_the_second_recipients_letter_fails_it_is_not_logged_with_the_first_recipients_text():
    from unittest import mock
    from violations import services
    from violations.models import EmailLog, Notice
    v = _case_with_owner_and_tenant()
    real = services.render_notice_pdf
    calls = {"n": 0}

    def flaky(notice, who):
        calls["n"] += 1
        if calls["n"] == 2:                                                                  # the tenant's copy fails
            raise RuntimeError("tenant copy failed")
        return real(notice, who)
    with mock.patch.object(services, "render_notice_pdf", side_effect=flaky):
        services.send_notice(v, Notice.Kind.VIOLATION, None, hold_mail_days=2)
    good, bad = EmailLog.objects.filter(violation=v, ok=True).get(), EmailLog.objects.filter(violation=v, ok=False).get()
    assert "owner@example.com" in good.to and "tenant@example.com" in bad.to
    assert "Dear Tina Tenant" in bad.body and "Dear Alex Owner" not in bad.body                     # the failure record names the tenant, not the owner
