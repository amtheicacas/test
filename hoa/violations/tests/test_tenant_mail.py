"""Cost control: paper mail goes to the owner only (MAIL_TO_TENANT off by default); the tenant is still emailed a copy (EMAIL_TO_TENANT on)."""
from datetime import date, timedelta
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core import mail as djmail
from django.utils import timezone

from violations import mailing, services, views
from violations.models import MailPiece, Notice, Owner, Profile, Violation, ViolationType

pytestmark = pytest.mark.django_db
S = Violation.Status
U = get_user_model()


@pytest.fixture
def rented(settings):
    settings.MAIL_PROVIDER, settings.LOB_API_KEY, settings.HOA_RETURN_LINE1, settings.HOA_RETURN_ZIP = "lob", "test_key", "PO Box 1", "34241"
    settings.HEARING_REGULAR_COPY = True
    o = Owner.objects.create(name="Pat Owner", property_address="12 Palm Way", city="Sarasota", state="FL", zip_code="34240", email="pat@example.com",
                             mailing_address="PO Box 55, Tampa, FL 33601", mail_line1="PO Box 55", mail_city="Tampa", mail_state="FL", mail_zip="33601", tenant_name="Tina Tenant", tenant_email="tina@example.com")
    t = ViolationType.objects.get_or_create(name="Shed", defaults={"default_cure_days": 15})[0]
    return Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c", status=S.HEARING_SCHEDULED, proposed_fine=100,
                                    board_meeting_date=date.today(), hearing_date=timezone.now() + timedelta(days=30), hearing_location="Clubhouse")


def lob_names(v, kind="hearing"):
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lambda to, pdf, d, extra=None: sent.append((to["name"], to["line1"], extra)) or {"id": f"l{len(sent)}"}):
        services.send_notice(v, kind)
    return sent


def test_by_default_only_the_owner_is_mailed_a_rented_home_gets_no_tenant_letter(rented):
    sent = lob_names(rented)
    assert {n for n, *_ in sent} == {"Pat Owner"} and len(sent) == 4              # owner mailing address + property, certified + first class each
    assert [e for *_, e in sent] == ["certified_return_receipt", None] * 2


def test_switching_it_on_mails_the_tenant_too(rented, settings):
    settings.MAIL_TO_TENANT = True
    sent = lob_names(rented)
    assert {n for n, *_ in sent} == {"Pat Owner", "Tina Tenant"} and len(sent) == 6


def test_the_tenant_is_still_emailed_a_copy_by_default_and_can_be_left_out(rented, settings):
    djmail.outbox.clear()
    with mock.patch.object(mailing, "lob_send", return_value={"id": "x"}):
        services.send_notice(rented, "hearing")
    assert sorted(m.to[0] for m in djmail.outbox) == ["pat@example.com", "tina@example.com"]
    settings.EMAIL_TO_TENANT = False
    djmail.outbox.clear()
    with mock.patch.object(mailing, "lob_send", return_value={"id": "x"}):
        services.send_notice(rented, "hearing")
    assert [m.to[0] for m in djmail.outbox] == ["pat@example.com"]


def test_by_hand_mode_lists_no_letter_for_the_tenant_by_default(rented, settings):
    settings.MAIL_PROVIDER = "manual"
    n = services.send_notice(rented, "hearing")
    assert sorted({p.label for p in n.pieces.all()}) == ["owner mailing", "owner property"]
    settings.MAIL_TO_TENANT = True
    n2 = Notice.objects.create(violation=rented, kind="hearing")
    mailing.plan_manual_pieces(n2)
    assert "tenant" in {p.label for p in n2.pieces.all()}


def test_the_decision_letter_is_certified_to_the_owner_only_by_default(rented):
    sent = lob_names(rented, "fine")
    assert {n for n, *_ in sent} == {"Pat Owner"} and all(e == "certified_return_receipt" for *_, e in sent) and len(sent) == 2


def test_the_new_violation_form_says_the_tenant_is_not_mailed(rented, settings):
    info = views.recipient_summary(rented.owner)
    assert info["tenant"]["mail"] == [] and info["tenant"]["skip_mail"] is True and info["tenant"]["emails"] == ["tina@example.com"]
    settings.MAIL_TO_TENANT = True
    info = views.recipient_summary(rented.owner)
    assert info["tenant"]["mail"] and info["tenant"]["skip_mail"] is False


def test_the_case_page_says_who_gets_what(rented, client, settings):
    u = U.objects.create_user("b1", password="pw-for-tests-123")
    Profile.objects.create(user=u, role="board")
    client.force_login(u)
    page = lambda: client.get(f"/violations/{rented.pk}/").content.decode()
    assert "Notices are mailed to the owner only; the tenant is emailed a copy." in page()
    settings.EMAIL_TO_TENANT = False
    assert "Notices go to the owner only." in page()
    settings.MAIL_TO_TENANT = True
    assert "Notices go to the owner and the tenant." in page()
