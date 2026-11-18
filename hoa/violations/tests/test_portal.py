import io
from datetime import date, timedelta
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.utils import timezone
from PIL import Image

from violations import accounts, mailing, services
from violations.models import CommitteeMeeting, Notice, Owner, Photo, Profile, Violation, ViolationType

S = Violation.Status
pytestmark = pytest.mark.django_db
U = get_user_model()


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def png():
    b = io.BytesIO()
    Image.new("RGB", (40, 30), "green").save(b, "PNG")
    return SimpleUploadedFile("a.png", b.getvalue(), content_type="image/png")


def make_case(addr, email="", **kw):
    o = Owner.objects.create(name=f"Owner {addr}", property_address=addr, email=email, zip_code="34240",
                             lat=27.3, lng=-82.5)
    t = ViolationType.objects.get_or_create(name="Shed")[0]
    v = Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c", **kw)
    Photo.objects.create(violation=v, image=png())
    return v


def login(client, role, owner=None):
    u = U.objects.create_user(f"{role}-{U.objects.count()}", password="pw-for-tests-123")
    Profile.objects.create(user=u, role=role, owner=owner)
    client.force_login(u)
    return u


# ---------- roles ----------
def test_homeowner_sees_only_own_cases(client):
    a, b = make_case("1 A St", status=S.NOTICE_SENT), make_case("2 B St", status=S.NOTICE_SENT)
    services.send_notice(a, Notice.Kind.VIOLATION)
    login(client, "homeowner", owner=a.owner)
    assert client.get("/").url == "/me/"
    body = client.get("/me/").content.decode()
    assert "Shed" in body and f"/me/{a.pk}/" in body and f"/me/{b.pk}/" not in body
    assert client.get(f"/me/{a.pk}/").status_code == 200
    assert client.get(f"/me/{b.pk}/").status_code == 404
    assert client.get(f"/photos/{b.photos.first().pk}/").status_code == 403
    assert client.get(f"/photos/{a.photos.first().pk}/").status_code == 200
    n = a.notices.first()
    assert client.get(f"/notices/{n.pk}/pdf/").content.startswith(b"%PDF")
    for staff_url in ["/violations/", "/board/", "/map/", "/meetings/", f"/violations/{a.pk}/", "/committee/"]:
        assert client.get(staff_url).status_code == 403, staff_url


def test_homeowner_cannot_open_another_owners_notice(client):
    a, b = make_case("1 A St", status=S.NOTICE_SENT), make_case("2 B St", status=S.NOTICE_SENT)
    nb = services.send_notice(b, Notice.Kind.VIOLATION)
    login(client, "homeowner", owner=a.owner)
    assert client.get(f"/notices/{nb.pk}/pdf/").status_code == 403


def test_homeowner_message_and_live_state(client):
    v = make_case("1 A St", status=S.NOTICE_SENT)
    login(client, "homeowner", owner=v.owner)
    sig1 = client.get(f"/me/{v.pk}/state/").json()["sig"]
    r = client.post(f"/me/{v.pk}/", {"message": "On it", "cure_reported": "on"})
    assert r.status_code == 302 and v.responses.get().cure_reported
    assert client.get(f"/me/{v.pk}/state/").json()["sig"] != sig1  # the open page would auto-refresh


def test_homeowner_cannot_see_draft(client):
    v = make_case("1 A St")  # draft
    login(client, "homeowner", owner=v.owner)
    assert client.get(f"/me/{v.pk}/").status_code == 404


def test_committee_sees_only_assigned_cases(client):
    assigned = make_case("1 A St", status=S.HEARING_SCHEDULED, proposed_fine=100)
    other = make_case("2 B St", status=S.NOTICE_SENT)
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=20), zoom_link="https://zoom.us/j/9")
    assigned.committee_meeting, assigned.hearing_date = m, m.starts_at
    assigned.save()
    login(client, "committee")
    assert client.get("/").url == "/committee/"
    assert client.get(f"/committee/meetings/{m.pk}/").status_code == 200
    assert client.get(f"/committee/cases/{assigned.pk}/").status_code == 200
    assert client.get(f"/committee/cases/{other.pk}/").status_code == 403
    assert client.get(f"/photos/{other.photos.first().pk}/").status_code == 403
    for url in ["/violations/", "/board/", "/map/", "/meetings/", f"/violations/{assigned.pk}/", "/api/map/"]:
        assert client.get(url).status_code == 403, url
    client.post(f"/committee/cases/{assigned.pk}/vote/", {"votes_for": 3, "votes_against": 0})
    assigned.refresh_from_db()
    assert assigned.status == S.HEARING_SCHEDULED  # hearing hasn't happened yet, so no vote


def test_committee_can_vote_after_meeting(client):
    v = make_case("1 A St", status=S.HEARING_SCHEDULED, proposed_fine=100)
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() - timedelta(hours=1), zoom_link="https://z.us/1")
    v.committee_meeting, v.hearing_date = m, m.starts_at
    v.save()
    login(client, "committee")
    client.post(f"/committee/cases/{v.pk}/vote/", {"votes_for": 3, "votes_against": 0})
    v.refresh_from_db()
    assert v.status == S.LEDGER_PENDING and v.fine_amount == 100  # awaits Board/manager approval before the letter


@pytest.mark.parametrize("role", ["manager", "board"])
def test_board_and_manager_see_everything(client, role):
    v = make_case("1 A St", status=S.NOTICE_SENT)
    login(client, role)
    for url in ["/", "/violations/", f"/violations/{v.pk}/", "/board/", "/meetings/", "/map/", "/api/map/", "/committee/"]:
        assert client.get(url).status_code == 200, url


def test_superuser_without_profile_is_manager(client):
    client.force_login(U.objects.create_superuser("root", password="pw-for-tests-123"))
    assert client.get("/").status_code == 200


def test_user_with_no_role_is_denied(client):
    client.force_login(U.objects.create_user("nobody", password="pw-for-tests-123"))
    assert client.get("/").status_code == 403


def test_invite_creates_homeowner_login():
    o = Owner.objects.create(name="Pat", property_address="9 X St", email="pat@example.com")
    req = RequestFactory().get("/")
    assert accounts.invite_owner(o, req) is True
    assert accounts.invite_owner(o, req) is False  # already invited
    assert Profile.objects.get().role == "homeowner"
    assert len(mail.outbox) == 1 and "/accounts/reset/" in mail.outbox[0].body


# ---------- hold / legal ----------
def test_hold_pauses_cure_clock_and_resumes():
    v = make_case("1 A St", status=S.NOTICE_SENT, cure_deadline=date.today() + timedelta(days=5))
    with pytest.raises(services.WorkflowError):
        services.put_on_hold(v, " ")
    services.put_on_hold(v, "Owner hospitalized")
    v.held_on = date.today() - timedelta(days=10)
    v.save()
    assert services.advance_expired() == 0
    services.resume(v)
    v.refresh_from_db()
    assert v.status == S.NOTICE_SENT and v.cure_deadline == date.today() + timedelta(days=15)


def test_send_to_legal_from_open_stage():
    v = make_case("1 A St", status=S.PENDING_BOARD)
    services.refer_to_attorney(v)
    v.refresh_from_db()
    assert v.status == S.REFERRED_LEGAL and v.stage_flag == "Sent to legal"


def test_stage_tracker():
    v = make_case("1 A St", status=S.HEARING_SCHEDULED)
    assert [st for _, st in v.stage_steps()] == ["done", "done", "done", "current", "todo", "todo"]
    v.status = S.CLOSED
    assert v.stage_steps()[-1][1] == "done"


# ---------- committee batches ----------
def test_batch_assign_sends_all_waiting_and_skips_too_soon(mailoutbox):
    for i in range(3):
        make_case(f"{i} A St", email=f"o{i}@x.com", status=S.HEARING_NEEDED, proposed_fine=50,
                  board_meeting_date=date.today())
    soon = services.create_meeting(timezone.now() + timedelta(days=3), "https://z.us/3")
    assert services.assign_all_to_meeting(soon)[1]
    assert Violation.objects.filter(status=S.HEARING_NEEDED).count() == 3
    m = services.create_meeting(timezone.now() + timedelta(days=20), "https://z.us/4")
    done, skipped = services.assign_all_to_meeting(m)
    assert len(done) == 3 and not skipped
    assert all(c.status == S.HEARING_SCHEDULED for c in Violation.objects.all())
    assert len(mailoutbox) == 3 and m.violations.count() == 3
    assert all(o.attachments[0][2] == "application/pdf" for o in mailoutbox)


def test_meeting_needs_zoom_or_location():
    with pytest.raises(services.WorkflowError):
        services.create_meeting(timezone.now() + timedelta(days=20))


# ---------- print/mail provider ----------
def test_manual_provider_does_nothing(settings):
    settings.MAIL_PROVIDER = "manual"
    v = make_case("1 A St", status=S.NOTICE_SENT, cure_deadline=date.today())
    n = services.send_notice(v, Notice.Kind.VIOLATION)
    assert n.mailed_on is None and n.provider == ""


def _lob(settings):
    settings.MAIL_PROVIDER, settings.LOB_API_KEY = "lob", "test_key"
    settings.HOA_RETURN_LINE1, settings.HOA_RETURN_ZIP = "1 HOA Way", "34240"


def test_lob_hearing_goes_certified_and_regular_to_owner_and_tenant(settings):
    _lob(settings)
    v = make_case("1 A St", status=S.HEARING_SCHEDULED, cure_deadline=date.today(), proposed_fine=100,
                  board_meeting_date=date.today(), hearing_date=timezone.now() + timedelta(days=20),
                  hearing_location="Zoom")
    v.owner.tenant_name = "Tina"
    v.owner.save()
    sent = []

    def fake(to, pdf, desc, extra=None):
        sent.append((to["name"], extra))
        return {"id": f"ltr_{len(sent)}", "tracking_number": "9400" if extra else None}
    with mock.patch.object(mailing, "lob_send", side_effect=fake):
        n = services.send_notice(v, Notice.Kind.HEARING)
    assert sent == [("Owner 1 A St", "certified_return_receipt"), ("Owner 1 A St", None),
                    ("Tina", "certified_return_receipt"), ("Tina", None)]
    n.refresh_from_db()
    assert n.provider == "lob" and n.method == "certified" and n.tracking_number == "9400" and n.mailed_on


def test_lob_failure_does_not_block_and_is_flagged(settings):
    _lob(settings)
    v = make_case("1 A St", status=S.NOTICE_SENT, cure_deadline=date.today())
    with mock.patch.object(mailing, "lob_send", side_effect=RuntimeError("boom")):
        n = services.send_notice(v, Notice.Kind.VIOLATION)
    n.refresh_from_db()
    assert n.mailed_on is None and "boom" in n.provider_note


def test_lob_request_shape(settings):
    _lob(settings)
    resp = mock.MagicMock()
    resp.__enter__.return_value = mock.MagicMock(read=lambda *_: b'{"id":"ltr_1"}')
    to = {"name": "Pat", "line1": "1 A St", "city": "Sarasota", "state": "FL", "zip": "34240"}
    with mock.patch("urllib.request.urlopen", return_value=resp) as m:
        mailing.lob_send(to, b"%PDF-1", "d", "certified")
    req = m.call_args[0][0]
    assert req.full_url == "https://api.lob.com/v1/letters" and req.get_header("Authorization").startswith("Basic ")
    assert b'name="extra_service"' in req.data and b"certified" in req.data and b"%PDF-1" in req.data
    with pytest.raises(ValueError):
        mailing.lob_send({**to, "zip": ""}, b"x", "d")


# ---------- map / search / create ----------
def test_map_api_colors_and_data(client):
    make_case("1 A St", status=S.HEARING_SCHEDULED)
    make_case("2 A St", status=S.NOTICE_SENT)
    make_case("3 A St", status=S.CURED)
    Owner.objects.create(name="None", property_address="4 A St", lat=27.31, lng=-82.51)
    Owner.objects.create(name="Unmapped", property_address="5 A St")
    login(client, "board")
    homes = {h["address"]: h for h in client.get("/api/map/").json()["homes"]}
    assert [homes[a]["color"] for a in ["1 A St", "2 A St", "3 A St", "4 A St"]] == ["red", "orange", "green", "gray"]
    assert homes["5 A St"]["lat"] is None and homes["5 A St"]["color"] == "gray"  # no street coordinates yet, but still listed (the site plan needs no coordinates)
    assert homes["3 A St"]["resolved"] == 1
    assert client.get("/map/").status_code == 200


def test_search_filters(client):
    make_case("1 Palm Way", status=S.NOTICE_SENT)
    make_case("2 Oak Ln", status=S.CURED)
    login(client, "manager")
    body = client.get("/violations/?q=palm").content.decode()
    assert "Palm Way" in body and "Oak Ln" not in body
    body = client.get("/violations/?status=open").content.decode()
    assert "Palm Way" in body and "Oak Ln" not in body


def test_create_issues_immediately(client):
    login(client, "manager")
    o = Owner.objects.create(name="Pat", property_address="9 X St", email="pat@example.com")
    t = ViolationType.objects.create(name="Shed")
    r = client.post("/violations/new/", {"owner": o.pk, "vtype": t.pk, "observed_on": date.today().isoformat(),
                                         "description": "d", "cure_required": "c", "cure_days": 15, "timing": "standard",
                                         "photos": png()})
    v = Violation.objects.get()
    assert r.status_code == 302 and v.status == S.NOTICE_SENT and len(mail.outbox) == 1
