"""Committee approval -> saved -> Board/manager approves -> posted to the ledger -> Notice of Imposition to owner (and tenant)."""
import io
import json
from datetime import date, timedelta
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image

from violations import ledger, services, vantaca
from violations.models import Notice, Owner, Photo, Profile, Violation, ViolationType

S = Violation.Status
pytestmark = pytest.mark.django_db
U = get_user_model()
TODAY = date.today()


@pytest.fixture(autouse=True)
def _s(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.HOA_PAYMENT_ADDRESS = "c/o the Treasurer, PO Box 9"


def png():
    b = io.BytesIO()
    Image.new("RGB", (30, 20)).save(b, "PNG")
    return SimpleUploadedFile("a.png", b.getvalue(), content_type="image/png")


def pending(tenant=False, fine=150, suspension="", ext="V-77"):
    o = Owner.objects.create(name="Pat Owner", property_address=f"{Owner.objects.count() + 1} Palm Way", email="p@example.com",
                             external_id=ext, tenant_name="Tina Tenant" if tenant else "", tenant_email="t@example.com" if tenant else "")
    t = ViolationType.objects.get_or_create(name="Shed", defaults={"default_cure_days": 15})[0]
    v = Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c", status=S.LEDGER_PENDING, fine_amount=fine,
                                 proposed_fine=fine, suspension=suspension, proposed_suspension=suspension,
                                 suspension_days=30 if suspension else None, proposed_suspension_days=30 if suspension else None,
                                 committee_votes_for=3, committee_votes_against=0, board_meeting_date=TODAY - timedelta(days=30),
                                 hearing_date=timezone.now() - timedelta(days=1), cure_deadline=TODAY - timedelta(days=40))
    Photo.objects.create(violation=v, image=png())
    Notice.objects.create(violation=v, kind="violation")
    return v


def login(client, role):
    u = U.objects.create_user(f"{role}{U.objects.count()}", password="pw-for-tests-123")
    Profile.objects.create(user=u, role=role)
    client.force_login(u)


# ---------- gate ----------
def test_committee_vote_saves_the_decision_and_sends_nothing():
    v = pending()
    v.status = S.HEARING_SCHEDULED
    v.fine_amount = None
    v.save()
    mail.outbox.clear()
    services.committee_decide(v, 3, 0)
    v.refresh_from_db()
    assert v.status == S.LEDGER_PENDING and v.fine_amount == 150 and mail.outbox == []
    assert not v.notices.filter(kind=Notice.Kind.FINE).exists() and v.ledger_posted_on is None


def test_nothing_to_finalize_unless_pending():
    v = pending()
    v.status = S.HEARING_SCHEDULED
    v.save()
    with pytest.raises(services.WorkflowError):
        services.finalize_imposition(v)


# ---------- manual posting ----------
def test_manual_posting_records_reference_sets_due_date_and_sends_the_letter_to_owner_and_tenant():
    v = pending(tenant=True)
    mail.outbox.clear()
    n = services.finalize_imposition(v, method="manual", reference="TX-5521", today=date(2026, 12, 1))
    v.refresh_from_db()
    assert v.status == S.FINE_APPROVED and v.ledger_posted_on == date(2026, 12, 1) and v.ledger_reference == "TX-5521"
    assert v.fine_due_on == date(2026, 12, 6)
    assert n.kind == Notice.Kind.FINE
    assert sorted(m.to[0] for m in mail.outbox) == ["p@example.com", "t@example.com"]
    assert all("imposition" in m.subject.lower() for m in mail.outbox)
    assert v.events.filter(text__contains="recorded as posted in Vantaca by hand").exists()


def test_letter_says_the_fine_was_posted_and_when_it_is_due():
    import shutil
    import subprocess
    if not shutil.which("pdftotext"):
        pytest.skip("pdftotext not installed")
    v = pending()
    n = services.finalize_imposition(v, reference="TX-1", today=date(2026, 12, 1))
    t = " ".join(subprocess.run(["pdftotext", "-", "-"], input=services.render_notice_pdf(n), capture_output=True).stdout.decode().split())
    assert "has been posted to your homeowner account ledger on December 01, 2026" in t
    assert "by December 06, 2026" in t and "c/o the Treasurer, PO Box 9" in t


def test_suspension_only_skips_the_ledger_but_still_sends_the_letter():
    v = pending(fine=None, suspension="the pool")
    mail.outbox.clear()
    with mock.patch.object(vantaca, "api_request") as api:
        services.finalize_imposition(v, method="api")
    v.refresh_from_db()
    assert not api.called and v.ledger_posted_on is None and v.status == S.FINE_APPROVED and v.fine_due_on is None
    assert v.suspension_effective_on == TODAY and len(mail.outbox) == 1


# ---------- Vantaca API posting ----------
def cfg(settings):
    settings.VANTACA_BASE_URL, settings.VANTACA_USER, settings.VANTACA_PASSWORD = "https://api.example", "u", "p"
    settings.VANTACA_LEDGER_POST_PATH, settings.VANTACA_FINE_CHARGE_CODE = "write/charges", "FINE-100"


def test_ledger_is_not_configured_by_default():
    assert not ledger.configured()
    with pytest.raises(ledger.LedgerError, match="isn't set up"):
        ledger.post_fine(pending(), TODAY)


def test_api_posting_sends_the_right_charge_and_stores_the_reference(settings):
    cfg(settings)
    v = pending()
    with mock.patch.object(vantaca, "api_request", return_value={"id": "LEDGER-9001"}) as api:
        services.finalize_imposition(v, method="api", today=date(2026, 12, 1))
    method, path, body = api.call_args[0]
    assert (method, path) == ("POST", "write/charges")
    assert body == {"accountId": "V-77", "amount": 150.0, "chargeCode": "FINE-100", "date": "2026-12-01",
                    "description": f"HOA fine - Shed (case #{v.pk})", "reference": f"HOA-VIOL-{v.pk}"}
    v.refresh_from_db()
    assert v.ledger_reference == "LEDGER-9001" and v.status == S.FINE_APPROVED


def test_api_failure_posts_nothing_sends_nothing_and_can_be_retried(settings):
    cfg(settings)
    v = pending()
    mail.outbox.clear()
    with mock.patch.object(vantaca, "api_request", side_effect=RuntimeError("503")):
        with pytest.raises(services.WorkflowError, match="did not accept"):
            services.finalize_imposition(v, method="api")
    v.refresh_from_db()
    assert v.status == S.LEDGER_PENDING and v.ledger_posted_on is None and mail.outbox == []
    with mock.patch.object(vantaca, "api_request", return_value={}):
        services.finalize_imposition(v, method="api")
    v.refresh_from_db()
    assert v.status == S.FINE_APPROVED and v.ledger_reference == f"HOA-VIOL-{v.pk}"  # falls back to our stable reference


def test_api_needs_the_owners_vantaca_account_id(settings):
    cfg(settings)
    v = pending(ext=None)
    with pytest.raises(services.WorkflowError, match="account ID"):
        services.finalize_imposition(v, method="api")
    assert v.status == S.LEDGER_PENDING


def test_a_fine_is_never_posted_twice(settings):
    cfg(settings)
    v = pending()
    v.ledger_posted_on, v.ledger_reference = TODAY, "EARLIER"
    v.save()
    with mock.patch.object(vantaca, "api_request") as api:
        services.finalize_imposition(v, method="api")  # e.g. a retry after the letter step failed earlier
    assert not api.called
    v.refresh_from_db()
    assert v.ledger_reference == "EARLIER" and v.status == S.FINE_APPROVED


def test_description_with_quotes_stays_valid_json(settings):
    cfg(settings)
    v = pending()
    v.vtype.name = 'Shed "temporary" \\ structure'
    v.vtype.save()
    json.dumps(ledger.build_body(v, TODAY))  # no exception


def test_email_failure_does_not_stop_the_imposition(settings):
    v = pending()
    with mock.patch("django.core.mail.EmailMessage.send", side_effect=OSError("smtp down")):
        n = services.finalize_imposition(v)
    v.refresh_from_db()
    n.refresh_from_db()
    assert v.status == S.FINE_APPROVED and n.emailed_at is None and "smtp down" in n.email_error


# ---------- while waiting ----------
def test_marking_cured_while_pending_keeps_the_fine():
    v = pending()
    services.mark_cured(v)
    v.refresh_from_db()
    assert v.status == S.LEDGER_PENDING and v.cured_on == TODAY
    services.finalize_imposition(v)
    services.record_payment(v, TODAY)
    v.refresh_from_db()
    assert v.status == S.CLOSED  # corrected and paid


def test_cannot_refer_to_legal_before_the_fine_is_posted():
    with pytest.raises(services.WorkflowError):
        services.refer_to_attorney(pending())


# ---------- who can approve, and where it shows ----------
def test_board_and_manager_can_approve_but_committee_and_homeowner_cannot(client):
    v = pending()
    login(client, "committee")
    assert client.post(f"/violations/{v.pk}/impose/", {"method": "manual"}).status_code == 403
    login(client, "homeowner")
    assert client.post(f"/violations/{v.pk}/impose/", {"method": "manual"}).status_code == 403
    v.refresh_from_db()
    assert v.status == S.LEDGER_PENDING
    login(client, "board")
    page = client.get(f"/violations/{v.pk}/").content.decode()
    assert "Committee decision awaiting your approval" in page and "Automatic posting to Vantaca isn&#x27;t set up" in page.replace("isn't", "isn&#x27;t")
    client.post(f"/violations/{v.pk}/impose/", {"method": "manual", "reference": "R1"})
    v.refresh_from_db()
    assert v.status == S.FINE_APPROVED and v.ledger_reference == "R1"


def test_pending_decisions_show_on_dashboard_and_board_queue(client):
    v = pending()
    login(client, "manager")
    assert "awaiting approval to post" in client.get("/").content.decode()
    assert f"#{v.pk}" in client.get("/board/").content.decode()


def test_homeowner_does_not_see_the_fine_until_it_is_approved(client):
    v = pending()
    o = v.owner
    u = U.objects.create_user("owner1", password="pw-for-tests-123")
    Profile.objects.create(user=u, role="homeowner", owner=o)
    client.force_login(u)
    page = client.get(f"/me/{v.pk}/").content.decode()
    assert "Fine imposed" not in page and "$150" not in page
    services.finalize_imposition(v)
    assert "Fine imposed: $150" in client.get(f"/me/{v.pk}/").content.decode()
