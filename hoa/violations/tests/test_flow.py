import io
from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image

from violations import services
from violations.models import Notice, Owner, Photo, Violation, ViolationType

S = Violation.Status
pytestmark = pytest.mark.django_db


def png(name="a.png"):
    b = io.BytesIO()
    Image.new("RGB", (40, 30), "green").save(b, "PNG")
    return SimpleUploadedFile(name, b.getvalue(), content_type="image/png")


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def user():
    return get_user_model().objects.create_user("mgr", password="pw-for-tests-123", is_staff=True)


@pytest.fixture
def violation(user):
    o = Owner.objects.create(name="Pat Owner", property_address="12 Palm Way", email="pat@example.com")
    t = ViolationType.objects.create(name="Unapproved shed", reference="Decl. Art. VII §3")
    v = Violation.objects.create(owner=o, vtype=t, description="Shed in side yard", cure_required="Remove shed",
                                 cure_days=14, created_by=user)
    Photo.objects.create(violation=v, image=png())
    return v


def test_notice_requires_photo(user):
    o = Owner.objects.create(name="A", property_address="1 X")
    t = ViolationType.objects.create(name="T")
    v = Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c")
    with pytest.raises(services.WorkflowError):
        services.issue_first_notice(v, user)


def test_notice_emails_pdf_and_sets_deadline(violation, user):
    n = services.issue_first_notice(violation, user, today=date(2026, 3, 1))
    violation.refresh_from_db()
    assert violation.cure_deadline == date(2026, 3, 15) and violation.status == S.NOTICE_SENT
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["pat@example.com"]
    assert mail.outbox[0].attachments[0][2] == "application/pdf"
    assert violation.token in mail.outbox[0].body
    assert n.emailed_at and n.mailed_on is None
    services.mark_mailed(n, date(2026, 3, 2), "9400-1", user)
    n.refresh_from_db()
    assert n.tracking_number == "9400-1"


def test_no_email_is_mail_only(violation, user):
    violation.owner.email = ""
    violation.owner.save()
    n = services.issue_first_notice(violation, user)
    assert mail.outbox == [] and n.emailed_at is None


def _to_board(violation, user):
    services.issue_first_notice(violation, user, today=date.today() - timedelta(days=20))
    assert services.advance_expired() == 1
    violation.refresh_from_db()
    assert violation.status == S.PENDING_BOARD


def test_full_path_to_imposed_fine_paid_and_closed(violation, user):
    _to_board(violation, user)
    with pytest.raises(services.WorkflowError):  # board must name a fine and/or suspension
        services.board_decide(violation, "proceed", date.today(), user, votes_for=3, votes_against=0)
    with pytest.raises(services.WorkflowError):  # over the $1,000 total cap
        services.board_decide(violation, "proceed", date.today(), user, fine=1500, votes_for=3, votes_against=0)
    with pytest.raises(services.WorkflowError):  # needs a majority of the Board
        services.board_decide(violation, "proceed", date.today(), user, fine=250, votes_for=1, votes_against=2)
    services.board_decide(violation, "proceed", date.today(), user, fine=250, votes_for=3, votes_against=1)
    violation.refresh_from_db()
    assert violation.status == S.HEARING_NEEDED and violation.proposed_fine == 250
    soon = services.create_meeting(timezone.now() + timedelta(days=5), "https://zoom.us/j/1", user=user)
    with pytest.raises(services.WorkflowError):  # 14-day rule
        services.assign_to_meeting(violation, soon, user)
    m = services.create_meeting(timezone.now() + timedelta(days=15), "https://zoom.us/j/2", user=user)
    n = services.assign_to_meeting(violation, m, user)
    assert n.kind == Notice.Kind.HEARING and "zoom.us/j/2" in violation.hearing_location
    assert services.render_notice_pdf(n).startswith(b"%PDF")
    with pytest.raises(services.WorkflowError):  # hearing hasn't happened yet
        services.committee_decide(violation, 3, 0, user)
    Violation.objects.filter(pk=violation.pk).update(hearing_date=timezone.now() - timedelta(hours=1))
    violation.refresh_from_db()
    with pytest.raises(services.WorkflowError):  # fewer than 3 members voting
        services.committee_decide(violation, 2, 0, user)
    services.committee_decide(violation, 2, 1, user, today=date(2026, 5, 1))
    violation.refresh_from_db()
    assert violation.status == S.LEDGER_PENDING and violation.fine_amount == 250  # saved, awaiting Board/manager approval
    assert not violation.notices.filter(kind=Notice.Kind.FINE).exists()  # no letter yet
    services.finalize_imposition(violation, user, today=date(2026, 5, 1))
    violation.refresh_from_db()
    assert violation.status == S.FINE_APPROVED and violation.ledger_posted_on == date(2026, 5, 1)
    assert violation.fine_due_on == date(2026, 5, 6)  # due 5 days after the letter
    assert violation.notices.filter(kind=Notice.Kind.FINE).exists()
    services.mark_cured(violation, user)
    violation.refresh_from_db()
    assert violation.status == S.FINE_APPROVED  # still unpaid
    services.record_payment(violation, date.today(), user)
    violation.refresh_from_db()
    assert violation.status == S.CLOSED


def test_suspension_only_and_legal_referral(violation, user):
    _to_board(violation, user)
    services.board_decide(violation, "proceed", date.today(), user, suspension="the pool and clubhouse", suspension_days=30,
                          votes_for=3, votes_against=0)
    Violation.objects.filter(pk=violation.pk).update(status=S.HEARING_SCHEDULED,
                                                     hearing_date=timezone.now() - timedelta(days=1))
    violation.refresh_from_db()
    services.committee_decide(violation, 3, 0, user)
    violation.refresh_from_db()
    assert violation.status == S.LEDGER_PENDING
    services.finalize_imposition(violation, user)
    violation.refresh_from_db()
    assert violation.fine_amount is None and violation.suspension == "the pool and clubhouse" and violation.suspension_days == 30
    assert violation.suspension_effective_on == date.today()
    with pytest.raises(services.WorkflowError):
        services.record_payment(violation, date.today(), user)
    services.refer_to_attorney(violation, user)
    violation.refresh_from_db()
    assert violation.status == S.REFERRED_LEGAL


def test_committee_can_reject(violation, user):
    violation.status, violation.hearing_date = S.HEARING_SCHEDULED, timezone.now() - timedelta(days=1)
    violation.proposed_fine = 100
    violation.save()
    services.committee_decide(violation, 1, 2, user)
    violation.refresh_from_db()
    assert violation.status == S.FINE_REJECTED and violation.fine_amount is None


def test_safety_referral_skips_cure_wait(violation, user):
    services.issue_first_notice(violation, user)
    services.refer_urgent(violation, user, "exposed wiring")
    violation.refresh_from_db()
    assert violation.status == S.PENDING_BOARD and violation.safety_issue


def test_tenant_gets_notice_too(violation, user):
    violation.owner.tenant_name, violation.owner.tenant_email = "Tina Tenant", "tina@example.com"
    violation.owner.save()
    n = services.issue_first_notice(violation, user)
    assert sorted(m.to[0] for m in mail.outbox) == ["pat@example.com", "tina@example.com"]  # separate emails
    by = {m.to[0]: m for m in mail.outbox}
    assert by["pat@example.com"].body.startswith("Dear Pat Owner") and by["tina@example.com"].body.startswith("Dear Tina Tenant")
    assert services.render_notice_pdf(n, "tenant").startswith(b"%PDF")


def test_second_notice_resets_deadline(violation, user):
    services.issue_first_notice(violation, user)
    new = date.today() + timedelta(days=15)
    services.grant_extension(violation, new, user, second_notice=True)
    violation.refresh_from_db()
    assert violation.cure_deadline == new and violation.notices.filter(kind=Notice.Kind.SECOND).exists()


def test_extension_resets_clock(violation, user):
    services.issue_first_notice(violation, user, today=date.today() - timedelta(days=20))
    services.advance_expired()
    new = date.today() + timedelta(days=10)
    services.grant_extension(violation, new, user, "work underway")
    violation.refresh_from_db()
    assert violation.status == S.NOTICE_SENT and violation.cure_deadline == new
    assert services.advance_expired() == 0
    with pytest.raises(services.WorkflowError):
        services.grant_extension(violation, date.today() - timedelta(days=1), user)


def test_resident_response_flow(client, violation, user):
    services.issue_first_notice(violation, user)
    url = f"/r/{violation.token}/"
    assert client.get(url).status_code == 200
    r = client.post(url, {"message": "Working on it", "request_extension": "on", "extension_days": 10,
                          "cure_reported": "on", "photos": png()})
    assert r.status_code == 302
    resp = violation.responses.get()
    assert resp.extension_requested and resp.extension_days == 10 and resp.from_resident and resp.cure_reported
    client.force_login(violation.created_by)
    assert client.get('/').status_code == 200  # dashboard lists reinspect + awaiting-reply
    assert violation.photos.filter(from_resident=True).count() == 1
    assert client.get("/r/not-a-token/").status_code == 404


def test_fake_image_rejected(client, violation, user):
    services.issue_first_notice(violation, user)
    bad = SimpleUploadedFile("x.png", b"not an image", content_type="image/png")
    client.post(f"/r/{violation.token}/", {"message": "hi", "photos": bad})
    assert violation.photos.filter(from_resident=True).count() == 0


def test_draft_hidden_from_resident(client, violation):
    assert client.get(f"/r/{violation.token}/").status_code == 404


def test_staff_pages_require_login(client, violation):
    for u in ["/", "/violations/", f"/violations/{violation.pk}/", "/board/", f"/photos/{violation.photos.first().pk}/"]:
        assert client.get(u).status_code == 302, u


def test_staff_ui_end_to_end(client, user, violation):
    client.force_login(user)
    for u in ["/", "/violations/", "/violations/new/", f"/violations/{violation.pk}/", "/board/"]:
        assert client.get(u).status_code == 200, u
    client.post(f"/violations/{violation.pk}/send_notice/")
    violation.refresh_from_db()
    assert violation.status == S.NOTICE_SENT
    n = violation.notices.get()
    assert client.get(f"/notices/{n.pk}/pdf/").content.startswith(b"%PDF")
    assert client.get(f"/violations/{violation.pk}/").status_code == 200
