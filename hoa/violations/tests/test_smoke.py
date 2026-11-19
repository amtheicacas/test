"""Smoke tests: open every screen as every role, on a data set that touches every stage, and make sure nothing crashes (no 500s)
and that each role only reaches what it should."""
from datetime import date, timedelta

import pytest
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.utils import timezone

from violations import services
from violations.models import CommitteeMeeting, Notice, Owner, Photo, Profile, Response, Violation, ViolationType


@pytest.fixture
def world(db, django_user_model):
    call_command("load_standards")
    vt = ViolationType.objects.exclude(code__isnull=True).first()
    rented = Owner.objects.create(name="Rita Rented", second_owner_name="Ray Rented", property_address="1 Smoke Way", lot_no="1", email="r@example.com",
                                  mailing_address="PO Box 9, Tampa, FL 33601", tenant_name="Tim Tenant", tenant_email="t@example.com",
                                  tenant_end=date.today() + timedelta(days=60))
    plain = Owner.objects.create(name="Paul Plain", property_address="2 Smoke Way", lot_no="2", email="p@example.com")
    cases = {}

    def make(key, owner=plain, **kw):
        v = Violation.objects.create(owner=owner, vtype=vt, description="Observed.", cure_required="Fix it.", **kw)
        Photo.objects.create(violation=v, image=ContentFile(b"\xff\xd8\xff", name=f"{key}.jpg"), from_resident=False)
        Notice.objects.create(violation=v, kind="violation", emailed_to="x@example.com")
        cases[key] = v
        return v
    make("draft", status="draft")
    make("cure", rented, status="notice_sent", cure_deadline=date.today() + timedelta(days=5))
    make("board", status="pending_board", cure_deadline=date.today() - timedelta(days=3))
    make("pit", status="pending_board", non_curable=True, cure_days=0)
    make("hold", status="on_hold", held_from="notice_sent", hold_reason="Away", held_on=date.today(), hold_until=date.today() + timedelta(days=10))
    make("reinspect", status="notice_sent", reinspect_on=date.today() - timedelta(days=2), cure_deadline=date.today() + timedelta(days=4))
    make("legal", status="referred_legal", legal_path="direct", legal_reason="Hazard", legal_referred_on=date.today())
    make("needs_hearing", status="hearing_needed", proposed_fine=100, board_meeting_date=date.today())
    meeting = CommitteeMeeting.objects.create(starts_at=timezone.now() - timedelta(hours=2), zoom_link="https://z.us/1")
    make("scheduled", status="hearing_scheduled", proposed_fine=100, committee_meeting=meeting, hearing_date=meeting.starts_at)
    make("ledger", status="ledger_pending", fine_amount=100, proposed_fine=100, committee_meeting=meeting, hearing_date=meeting.starts_at,
         committee_outcome="confirm", committee_votes_for=3, committee_votes_against=0, committee_findings="Yes.")
    make("imposed", status="fine_approved", fine_amount=100, fine_due_on=date.today() + timedelta(days=30), committee_meeting=meeting,
         hearing_date=meeting.starts_at, committee_outcome="confirm")
    make("waived", status="waived", committee_meeting=meeting, committee_outcome="waive", board_outcome="waive")
    make("cured", status="cured", cured_on=date.today())
    Response.objects.create(violation=cases["cure"], message="I fixed it", from_resident=True, cure_reported=True)
    Response.objects.create(violation=cases["cure"], message="Thanks", from_resident=False)
    users = {}
    for role in ("manager", "board", "committee"):
        u = django_user_model.objects.create_user(role, password="x", first_name=role.title(), email=f"{role}@example.com")
        Profile.objects.update_or_create(user=u, defaults={"role": role})
        users[role] = u
    h = django_user_model.objects.create_user("homeowner", password="x", first_name="Rita")
    Profile.objects.update_or_create(user=h, defaults={"role": "homeowner", "owner": rented})
    users["homeowner"] = h
    return {"cases": cases, "meeting": meeting, "owners": (rented, plain), "users": users}


def staff_pages(w):
    c, m, (rented, _), = w["cases"], w["meeting"], w["owners"]
    pages = ["/", "/violations/", "/violations/?status=open", "/violations/?reinspect=1", "/violations/?overdue=board", "/violations/new/",
             "/violations/standards/search/?q=weeds", "/board/", "/meetings/", "/map/", "/api/map/", "/results/", f"/results/{m.pk}/",
             "/replies/", f"/owners/{rented.pk}/correspondence/", f"/committee/meetings/{m.pk}/", "/import/", "/import/template.csv", "/admin/"]
    pages += [f"/violations/{v.pk}/" for v in c.values()]
    pages += [f"/violations/{c['cure'].pk}/legal/", f"/violations/{c['legal'].pk}/legal/email/"]
    pages += [f"/notices/{n.pk}/pdf/" for n in Notice.objects.all()[:3]]
    return pages


@pytest.mark.parametrize("role", ["manager", "board"])
def test_every_staff_screen_opens(client, world, role):
    client.force_login(world["users"][role])
    for url in staff_pages(world):
        r = client.get(url, follow=True)
        expected = 403 if (role == "board" and url.startswith("/import/")) else 200      # importing owners is the manager's job
        assert r.status_code == expected, (url, r.status_code)


def test_committee_screens_and_limits(client, world):
    c, m = world["cases"], world["meeting"]
    client.force_login(world["users"]["committee"])
    for url in ["/committee/", f"/committee/meetings/{m.pk}/", f"/committee/cases/{c['scheduled'].pk}/", f"/committee/cases/{c['needs_hearing'].pk}/"]:
        assert client.get(url).status_code == 200, url
    for url in ["/", "/violations/", "/board/", "/results/", f"/violations/{c['cure'].pk}/", "/replies/", "/import/", f"/committee/cases/{c['cure'].pk}/"]:
        r = client.get(url)
        assert r.status_code in (302, 403), (url, r.status_code)


def test_homeowner_screens_and_limits(client, world):
    c = world["cases"]
    client.force_login(world["users"]["homeowner"])
    for url in ["/me/", f"/me/{c['cure'].pk}/", "/me/household/", f"/me/{c['cure'].pk}/state/"]:
        assert client.get(url).status_code == 200, url
    assert client.get(f"/me/{c['draft'].pk}/").status_code == 404            # an owner never sees a draft
    other = c["board"]                                                       # belongs to the other owner
    assert client.get(f"/me/{other.pk}/").status_code in (403, 404)
    for url in ["/violations/", "/board/", "/results/", "/replies/", "/committee/", f"/violations/{c['cure'].pk}/"]:
        assert client.get(url).status_code in (302, 403), url


def test_emailed_private_link_pages_open(client, world):
    for key in ("cure", "hold"):
        v = world["cases"][key]
        assert client.get(f"/r/{v.token}/").status_code == 200


def test_the_daily_job_runs_on_this_data(world, mailoutbox):
    from django.core.management import call_command
    call_command("daily_jobs")
    assert services.lift_expired_holds() == 0


def test_every_notice_pdf_renders_for_every_recipient(world):
    from violations.pdf import render_notice_pdf
    for n in Notice.objects.all():
        for who in ("owner", "owner_property", "tenant"):
            assert render_notice_pdf(n, who)[:4] == b"%PDF"
    for kind in ("hearing", "fine", "extension", "second"):
        n = Notice.objects.create(violation=world["cases"]["imposed"], kind=kind)
        assert render_notice_pdf(n, "owner")[:4] == b"%PDF"
