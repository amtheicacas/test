"""Certified-mail tracking pulled in from the print/mail provider (Lob)."""
from datetime import date, timedelta
from unittest import mock

import pytest
from django.core.management import call_command

from violations import mailing, services
from violations.models import MailPiece, Notice, Owner, Profile, Violation, ViolationType


@pytest.fixture
def lob(settings):
    settings.MAIL_PROVIDER, settings.LOB_API_KEY = "lob", "test_key"
    settings.HOA_RETURN_LINE1, settings.HOA_RETURN_ZIP = "PO Box 1", "34241"
    settings.HEARING_REGULAR_COPY = False


@pytest.fixture
def hearing(db, lob):
    o = Owner.objects.create(name="Track Owner", property_address="1 Track Way", city="Sarasota", state="FL", zip_code="34240", email="t@example.com")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="T"), description="d", cure_required="c", status="hearing_scheduled",
                                 proposed_fine=100, board_meeting_date=date.today())
    n = Notice.objects.create(violation=v, kind="hearing")
    return n


EVENTS = {"id": "ltr_1", "tracking_number": "9407100000000000000001", "expected_delivery_date": "2026-12-01",
          "tracking_events": [{"name": "In Transit", "location": "TAMPA FL", "time": "2026-11-20T10:00:00Z"},
                              {"name": "Delivered", "location": "SARASOTA FL", "time": "2026-11-23T14:30:00Z"}]}


def send(n, response):
    with mock.patch.object(mailing, "lob_send", return_value=response):
        mailing.dispatch(n, lambda who: b"%PDF-1")


def test_sending_a_certified_letter_stores_the_tracking_number_on_a_mail_piece(hearing):
    send(hearing, {"id": "ltr_1", "tracking_number": "9407100000000000000001"})
    hearing.refresh_from_db()
    p = hearing.pieces.get()
    assert p.is_certified and p.tracking_number == "9407100000000000000001" and hearing.tracking_number == p.tracking_number
    assert "tools.usps.com" in p.usps_url and p.tracking_number in p.usps_url


def test_refresh_pulls_in_events_delivery_and_the_return_receipt_date(hearing):
    send(hearing, {"id": "ltr_1", "tracking_number": "9407100000000000000001"})
    with mock.patch.object(mailing, "lob_get", return_value=EVENTS):
        assert services.refresh_mail_tracking() == 1
    p = hearing.pieces.get(); hearing.refresh_from_db()
    assert p.status == "Delivered" and p.delivered_on == date(2026, 11, 23) and len(p.events) == 2 and not p.problem
    assert p.expected_delivery_on == date(2026, 12, 1)
    assert hearing.return_receipt_on == date(2026, 11, 23)
    with mock.patch.object(mailing, "lob_get", side_effect=AssertionError("delivered pieces are not asked again")):
        assert services.refresh_mail_tracking() == 0


def test_returned_mail_is_flagged_for_the_dashboard(hearing, client, django_user_model):
    send(hearing, {"id": "ltr_1", "tracking_number": "9407100000000000000002"})
    bad = {"tracking_events": [{"name": "Returned to Sender", "location": "SARASOTA FL", "time": "2026-11-25T09:00:00Z"}]}
    with mock.patch.object(mailing, "lob_get", return_value=bad):
        services.refresh_mail_tracking()
    assert hearing.pieces.get().problem
    u = django_user_model.objects.create_user("mg20", password="x"); Profile.objects.update_or_create(user=u, defaults={"role": "manager"})
    client.force_login(u)
    assert "Certified mail problems" in client.get("/").content.decode()


def test_provider_errors_never_break_the_job(hearing):
    send(hearing, {"id": "ltr_1", "tracking_number": "9407100000000000000003"})
    with mock.patch.object(mailing, "lob_get", side_effect=RuntimeError("Lob is down")):
        assert services.refresh_mail_tracking() == 0
        call_command("daily_jobs")


def test_case_page_shows_tracking_for_staff_and_the_owner(hearing, client, django_user_model):
    send(hearing, {"id": "ltr_1", "tracking_number": "9407100000000000000001"})
    with mock.patch.object(mailing, "lob_get", return_value=EVENTS):
        services.refresh_mail_tracking()
    v = hearing.violation
    u = django_user_model.objects.create_user("mg21", password="x"); Profile.objects.update_or_create(user=u, defaults={"role": "board"})
    client.force_login(u)
    html = client.get(f"/violations/{v.pk}/").content.decode()
    assert "9407100000000000000001" in html and "Track on USPS" in html and "Delivered Nov 23, 2026" in html and "Refresh tracking" in html
    h = django_user_model.objects.create_user("own8", password="x"); Profile.objects.update_or_create(user=h, defaults={"role": "homeowner", "owner": v.owner})
    client.force_login(h)
    html = client.get(f"/me/{v.pk}/").content.decode()
    assert "9407100000000000000001" in html and "Delivered" in html


def test_without_lob_tracking_is_entered_by_hand_and_refresh_says_so(db, client, django_user_model, settings):
    settings.MAIL_PROVIDER = "manual"
    o = Owner.objects.create(name="M", property_address="2 Hand Way")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="T2"), description="d", cure_required="c", status="notice_sent")
    n = Notice.objects.create(violation=v, kind="hearing")
    services.mark_mailed(n, date.today(), "9407999", method="certified")
    u = django_user_model.objects.create_user("mg22", password="x"); Profile.objects.update_or_create(user=u, defaults={"role": "manager"})
    client.force_login(u)
    r = client.post(f"/violations/{v.pk}/refresh_tracking/", follow=True)
    assert "Lob" in r.content.decode() and "9407999" in client.get(f"/violations/{v.pk}/").content.decode()
