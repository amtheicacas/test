"""Notice of Hearing by email + certified mail + first-class mail, mailed by hand: every letter is listed, its tracking number is recorded,
and a certified letter that could not be delivered shows at the top of the Board / manager home screen until someone handles it."""
import csv
import importlib
import io
import re
import zipfile
from datetime import date, timedelta
from unittest import mock

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core import mail as djmail
from django.utils import timezone

from violations import mailing, services
from violations.models import CommitteeMeeting, Event, MailPiece, Notice, Owner, Profile, Violation, ViolationType

pytestmark = pytest.mark.django_db
U = get_user_model()
S = Violation.Status
K = Notice.Kind
TODAY = date.today()
NUM = "9407 1118 9876 5432 1098 76"
NUM_CLEAN = "9407111898765432109876"


@pytest.fixture(autouse=True)
def manual_mail(settings):
    settings.MAIL_PROVIDER, settings.HEARING_REGULAR_COPY = "manual", True


def staff(name="boss", role="board"):
    u = U.objects.create_user(name, f"{name}@example.com", "pw-for-tests-123")
    Profile.objects.update_or_create(user=u, defaults={"role": role})
    return u


def case(mailing_address="", tenant=False, n=None):
    n = n if n is not None else Owner.objects.count() + 1
    o = Owner.objects.create(name=f"Owner {n}", property_address=f"{n} Certified Way", city="Sarasota", state="FL", zip_code="34240",
                             email=f"o{n}@example.com", mailing_address=mailing_address,
                             tenant_name="Tina Tenant" if tenant else "", tenant_email="tina@example.com" if tenant else "")
    t = ViolationType.objects.get_or_create(name="Shed", defaults={"default_cure_days": 15})[0]
    return Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c", status=S.HEARING_SCHEDULED, proposed_fine=100,
                                    board_meeting_date=TODAY, hearing_date=timezone.now() + timedelta(days=30), hearing_location="Clubhouse")


def hearing(v):
    return services.send_notice(v, K.HEARING)


def pieces(n):
    return list(n.pieces.order_by("pk"))


# ---------- the letters to be mailed are listed: certified + first class, for each address ----------
def test_a_hearing_notice_lists_a_certified_and_a_first_class_letter_to_mail():
    n = hearing(case())
    ps = pieces(n)
    assert [(p.service, p.label, p.by_hand, p.to_mail) for p in ps] == [("certified", "owner property", True, True), ("first_class", "owner property", True, True)]
    assert all(p.status == "To be mailed" and not p.is_mailed for p in ps)
    assert len(djmail.outbox) >= 1                                          # the email still goes out as before


def test_each_address_gets_its_own_pair_of_letters(settings):
    v = case(mailing_address="PO Box 5\nVenice, FL 34285", tenant=True)
    ps = pieces(hearing(v))
    assert sorted((p.label, p.service) for p in ps) == sorted([(lab, svc) for lab in ("owner mailing", "owner property", "tenant") for svc in ("certified", "first_class")])


def test_without_the_extra_first_class_copy_only_the_certified_letter_is_listed(settings):
    settings.HEARING_REGULAR_COPY = False
    assert [p.service for p in pieces(hearing(case()))] == ["certified"]


def test_the_decision_letter_is_certified_only_and_the_first_notice_is_not_listed():
    v = case()
    assert [p.service for p in pieces(services.send_notice(v, K.FINE))] == ["certified"]
    assert pieces(services.send_notice(v, K.VIOLATION)) == []             # first notices behave exactly as before


def test_listing_the_letters_twice_does_not_duplicate_them():
    n = hearing(case())
    mailing.plan_manual_pieces(n)
    mailing.dispatch(n, lambda who: b"x")
    assert n.pieces.count() == 2


def test_a_moved_hearing_makes_the_old_unmailed_letters_not_needed():
    v = case()
    old = hearing(v)
    new = hearing(v)
    assert all(p.handled_on and not p.to_mail and p.status == "Not mailed (not needed)" for p in pieces(old))
    assert all(p.to_mail for p in pieces(new))


def test_a_cancelled_hearing_makes_its_unmailed_letters_not_needed():
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=30), zoom_link="https://z.example/1")
    v = case()
    v.committee_meeting = m
    v.save()
    n = hearing(v)
    services.cancel_meeting(m, notify=False)
    assert all(p.handled_on and not p.to_mail for p in pieces(n))


# ---------- recording the mailing and the tracking number ----------
def test_a_certified_letter_cannot_be_marked_mailed_without_its_tracking_number():
    cert = pieces(hearing(case()))[0]
    with pytest.raises(services.WorkflowError, match="tracking number"):
        services.record_piece_mailed(cert, TODAY, "")
    cert.refresh_from_db()
    assert not cert.is_mailed


@pytest.mark.parametrize("bad", ["abc", "12 34", "94071118;DROP TABLE", "<script>alert(1)</script>", "9" * 41])
def test_a_tracking_number_that_is_not_letters_and_numbers_is_refused(bad):
    cert = pieces(hearing(case()))[0]
    with pytest.raises(services.WorkflowError):
        services.record_piece_mailed(cert, TODAY, bad)


def test_the_tracking_number_is_tidied_and_saved_on_the_letter_and_the_notice_and_the_case_history():
    n = hearing(case())
    cert = pieces(n)[0]
    services.record_piece_mailed(cert, TODAY, NUM.lower(), staff())
    cert.refresh_from_db(); n.refresh_from_db()
    assert cert.tracking_number == NUM_CLEAN and cert.is_mailed and cert.status == "Mailed" and "tools.usps.com" in cert.usps_url
    assert n.tracking_number == NUM_CLEAN                                    # the printed letter and the owner's page use this
    assert Event.objects.filter(violation=n.violation, text__contains=NUM_CLEAN).exists()


def test_the_notice_counts_as_mailed_only_when_every_letter_is_mailed():
    n = hearing(case())
    cert, first = pieces(n)
    services.record_piece_mailed(cert, TODAY, NUM)
    n.refresh_from_db()
    assert n.mailed_on is None and n.tracking_number == NUM_CLEAN              # the first-class copy is still to do
    services.record_piece_mailed(first, TODAY - timedelta(days=1), "")
    n.refresh_from_db()
    assert n.mailed_on == TODAY and n.method == "certified"                   # the certified date is the one that counts


def test_a_future_mailing_date_is_refused():
    first = pieces(hearing(case()))[1]
    with pytest.raises(services.WorkflowError, match="future"):
        services.record_piece_mailed(first, TODAY + timedelta(days=1), "")


def test_a_letter_can_be_corrected_after_it_was_recorded():
    n = hearing(case())
    cert = pieces(n)[0]
    services.record_piece_mailed(cert, TODAY, NUM)
    services.record_piece_mailed(cert, TODAY - timedelta(days=2), "9407 1118 9876 5432 1098 99")
    cert.refresh_from_db(); n.refresh_from_db()
    assert cert.mailed_on == TODAY - timedelta(days=2) and cert.tracking_number.endswith("99") and n.tracking_number.endswith("99")
    assert Event.objects.filter(violation=n.violation, text__contains="record corrected").exists()


def test_a_letter_handed_to_the_print_service_cannot_be_recorded_by_hand():
    n = hearing(case())
    p = MailPiece.objects.create(notice=n, provider_id="ltr_9", service="certified", label="owner property", mailed_on=TODAY)
    with pytest.raises(services.WorkflowError, match="print service"):
        services.record_piece_mailed(p, TODAY, NUM)


# ---------- delivered / not delivered / handled ----------
def mailed_cert(v=None):
    n = hearing(v or case())
    cert = pieces(n)[0]
    services.record_piece_mailed(cert, TODAY - timedelta(days=3), NUM)
    return n, cert


def test_delivery_sets_the_return_receipt_date_on_the_notice():
    n, cert = mailed_cert()
    services.record_piece_delivered(cert, TODAY - timedelta(days=1), staff())
    cert.refresh_from_db(); n.refresh_from_db()
    assert cert.delivered_on == TODAY - timedelta(days=1) and cert.status == "Delivered" and not cert.problem
    assert n.return_receipt_on == TODAY - timedelta(days=1)


def test_delivery_cannot_be_dated_before_the_mailing_or_recorded_before_it_was_mailed():
    n = hearing(case())
    cert = pieces(n)[0]
    with pytest.raises(services.WorkflowError, match="mailed first"):
        services.record_piece_delivered(cert, TODAY)
    services.record_piece_mailed(cert, TODAY - timedelta(days=3), NUM)
    with pytest.raises(services.WorkflowError, match="before it was mailed"):
        services.record_piece_delivered(cert, TODAY - timedelta(days=5))


def test_not_delivered_flags_the_letter_and_writes_a_warning_in_the_case_history():
    n, cert = mailed_cert()
    services.record_piece_not_delivered(cert, "unclaimed", TODAY, "Postal notice left, never picked up", staff())
    cert.refresh_from_db()
    assert cert.problem and cert.needs_attention and cert.status == "Unclaimed" and cert.problem_on == TODAY
    assert Event.objects.filter(violation=n.violation, text__startswith="Notice of committee hearing: WARNING").exists()


def test_not_delivered_needs_a_valid_reason_and_a_mailed_letter_and_not_one_already_delivered():
    n = hearing(case())
    cert = pieces(n)[0]
    with pytest.raises(services.WorkflowError, match="not been mailed"):
        services.record_piece_not_delivered(cert, "unclaimed")
    services.record_piece_mailed(cert, TODAY, NUM)
    with pytest.raises(services.WorkflowError, match="Choose"):
        services.record_piece_not_delivered(cert, "made-up")
    services.record_piece_delivered(cert, TODAY)
    with pytest.raises(services.WorkflowError, match="delivered"):
        services.record_piece_not_delivered(cert, "refused")


def test_handled_needs_a_note_and_clears_the_warning_but_keeps_the_record():
    n, cert = mailed_cert()
    services.record_piece_not_delivered(cert, "returned", TODAY)
    with pytest.raises(services.WorkflowError, match="what was done"):
        services.record_piece_handled(cert, "  ")
    services.record_piece_handled(cert, "Re-sent by first class mail and posted at the property", staff())
    cert.refresh_from_db()
    assert not cert.needs_attention and cert.problem and cert.status == "Returned to sender" and cert.handled_on == TODAY
    assert "Re-sent" in cert.handled_note


def test_a_new_not_delivered_result_reopens_a_handled_problem():
    n, cert = mailed_cert()
    services.record_piece_not_delivered(cert, "unclaimed", TODAY)
    services.record_piece_handled(cert, "Called the owner")
    services.record_piece_not_delivered(cert, "refused", TODAY, "Refused at the door")
    cert.refresh_from_db()
    assert cert.needs_attention and cert.handled_on is None and cert.status == "Refused"


def test_there_is_nothing_to_clear_on_a_letter_with_no_problem():
    n, cert = mailed_cert()
    with pytest.raises(services.WorkflowError, match="nothing to clear"):
        services.record_piece_handled(cert, "whatever")


def test_a_letter_that_was_never_needed_can_be_closed_and_then_the_notice_can_complete():
    n = hearing(case())
    cert, first = pieces(n)
    services.record_piece_mailed(cert, TODAY, NUM)
    services.record_piece_handled(first, "The owner picked it up in person")
    first.refresh_from_db(); n.refresh_from_db()
    assert not first.to_mail and first.status == "Not mailed (not needed)"
    assert n.mailed_on == TODAY                                                   # nothing else is waiting


# ---------- the home screen ----------
@pytest.mark.parametrize("role", ["board", "manager"])
def test_an_undelivered_certified_letter_is_flagged_at_the_top_of_the_home_screen(client, role):
    v = case()
    n, cert = mailed_cert(v)
    services.record_piece_not_delivered(cert, "unclaimed", TODAY)
    client.force_login(staff(f"b-{role}", role))
    html = client.get("/").content.decode()
    assert "Certified mail problems" in html and v.owner.property_address in html and "Unclaimed" in html and NUM_CLEAN in html
    assert html.index("Certified mail problems") < html.index("Violation Manager")       # above the page title: it is the first thing seen
    assert f"/violations/{v.pk}/#mailing" in html


def test_nothing_is_flagged_for_a_letter_that_is_fine_or_handled(client):
    n, cert = mailed_cert()
    client.force_login(staff())
    assert "Certified mail problems" not in client.get("/").content.decode()
    services.record_piece_not_delivered(cert, "refused", TODAY)
    assert "Certified mail problems" in client.get("/").content.decode()
    services.record_piece_handled(cert, "Owner emailed to say they will pick it up")
    assert "Certified mail problems" not in client.get("/").content.decode()


def test_only_certified_letters_raise_the_alert(client):
    n = hearing(case())
    first = pieces(n)[1]
    services.record_piece_mailed(first, TODAY, "")
    MailPiece.objects.filter(pk=first.pk).update(problem=True, status="Returned to sender")
    client.force_login(staff())
    assert "Certified mail problems" not in client.get("/").content.decode()


def test_the_home_screen_lists_letters_still_to_be_mailed_by_hand(client):
    v = case()
    hearing(v)
    client.force_login(staff())
    html = client.get("/").content.decode()
    assert "Letters to mail by hand" in html and v.owner.property_address in html and "Certified, return receipt to owner property" in html
    for p in MailPiece.objects.all():
        services.record_piece_mailed(p, TODAY, NUM if p.is_certified else "")
    assert "Letters to mail by hand" not in client.get("/").content.decode()


# ---------- the case page and the buttons (Board / manager only) ----------
def post(client, piece, action, **data):
    return client.post(f"/mail-pieces/{piece.pk}/{action}/", data, follow=True)


def test_the_case_page_shows_what_to_do_for_each_letter(client):
    v = case()
    n = hearing(v)
    client.force_login(staff())
    html = client.get(f"/violations/{v.pk}/").content.decode()
    assert 'id="mailing"' in html and "Mark mailed" in html and "Certified mail tracking number" in html and html.count("To be mailed") >= 2


def test_recording_the_mailing_through_the_page_works_and_a_missing_tracking_number_is_explained(client):
    v = case()
    n = hearing(v)
    cert, first = pieces(n)
    client.force_login(staff())
    r = post(client, cert, "mailed", mailed_on=TODAY.isoformat(), tracking_number="")
    cert.refresh_from_db()
    assert not cert.is_mailed and ("fill in every box" in r.content.decode() or "tracking number" in r.content.decode())
    r = post(client, cert, "mailed", mailed_on=TODAY.isoformat(), tracking_number=NUM)
    cert.refresh_from_db()
    assert cert.is_mailed and cert.tracking_number == NUM_CLEAN and "Mailing recorded" in r.content.decode()
    assert NUM_CLEAN in r.content.decode() and "Track on USPS" in r.content.decode()
    post(client, first, "mailed", mailed_on=TODAY.isoformat())
    first.refresh_from_db()
    assert first.is_mailed


def test_not_delivered_and_handled_through_the_page(client):
    v = case()
    n, cert = mailed_cert(v)
    client.force_login(staff())
    r = post(client, cert, "not_delivered", reason="unclaimed", on=TODAY.isoformat(), note="left a notice")
    cert.refresh_from_db()
    assert cert.needs_attention and "home screen" in r.content.decode() and "Mark handled" in r.content.decode()
    assert client.get("/").content.decode().count("Certified mail problems") >= 1
    post(client, cert, "handled", note="")                                                    # a note is required
    cert.refresh_from_db()
    assert cert.needs_attention
    post(client, cert, "handled", note="Sent again by first class mail")
    cert.refresh_from_db()
    assert not cert.needs_attention


def test_an_unknown_action_is_not_found_and_a_get_is_refused(client):
    n = hearing(case())
    cert = pieces(n)[0]
    client.force_login(staff())
    assert client.post(f"/mail-pieces/{cert.pk}/destroy/", {}).status_code == 404
    assert client.get(f"/mail-pieces/{cert.pk}/mailed/").status_code == 405


@pytest.mark.parametrize("role", ["committee", "homeowner"])
def test_committee_members_and_owners_cannot_record_anything(client, role):
    v = case()
    n = hearing(v)
    cert = pieces(n)[0]
    u = U.objects.create_user("x1", password="pw-for-tests-123")
    Profile.objects.create(user=u, role=role, owner=v.owner if role == "homeowner" else None)
    client.force_login(u)
    assert client.post(f"/mail-pieces/{cert.pk}/mailed/", {"mailed_on": TODAY.isoformat(), "tracking_number": NUM}).status_code in (302, 403)
    cert.refresh_from_db()
    assert not cert.is_mailed


def test_the_return_address_after_a_form_cannot_be_another_website(client):
    n = hearing(case())
    cert = pieces(n)[0]
    client.force_login(staff())
    r = client.post(f"/mail-pieces/{cert.pk}/mailed/", {"mailed_on": TODAY.isoformat(), "tracking_number": NUM, "next": "https://evil.example/x"})
    assert r.status_code == 302 and "evil" not in r["Location"]


# ---------- what the owner sees ----------
def test_the_owner_never_sees_staff_notes_or_letters_not_yet_mailed(client):
    v = case()
    n = hearing(v)
    cert = pieces(n)[0]
    owner_user = U.objects.create_user("own1", password="pw-for-tests-123")
    Profile.objects.create(user=owner_user, role="homeowner", owner=v.owner)
    client.force_login(owner_user)
    html = client.get(f"/me/{v.pk}/").content.decode()
    assert "To be mailed" not in html and "Still to be mailed" not in html          # nothing about a letter that has not gone out
    services.record_piece_mailed(cert, TODAY, NUM)
    services.record_piece_not_delivered(cert, "unclaimed", TODAY, "SECRET staff note about the owner")
    services.record_piece_handled(cert, "SECRET internal handling note")
    html = client.get(f"/me/{v.pk}/").content.decode()
    assert NUM_CLEAN in html and "Unclaimed" in html                                 # their own letter's tracking and result
    assert "SECRET" not in html and "Note on the" not in html


def test_the_staff_view_of_the_delivery_record_includes_the_notes():
    n, cert = mailed_cert()
    services.record_piece_not_delivered(cert, "unclaimed", TODAY, "left a notice on Nov 20")
    lines = "\n".join(n.delivery_lines(staff=True))
    assert "left a notice on Nov 20" in lines and "Unclaimed" in lines
    assert "left a notice" not in "\n".join(n.delivery_lines(staff=False))


# ---------- the case file ----------
def test_the_case_file_records_the_mailing_dates_tracking_and_delivery_problems():
    from violations import casefile
    n, cert = mailed_cert()
    services.record_piece_not_delivered(cert, "refused", TODAY, "refused at the door")
    data, _name = casefile.build_case_file(n.violation, staff())
    z = zipfile.ZipFile(io.BytesIO(data))
    name = next(x for x in z.namelist() if x.endswith("mailing/pieces-and-tracking.csv"))
    rows = list(csv.reader(io.StringIO(z.read(name).decode("utf-8-sig"))))
    head = rows[0]
    row = next(r for r in rows[1:] if r[head.index("Tracking number")] == NUM_CLEAN)
    assert row[head.index("Mailed by hand")] == "yes" and row[head.index("Latest status")] == "Refused"
    assert row[head.index("Problem noted on")] == TODAY.isoformat() and "refused at the door" in row[head.index("Staff notes")]


# ---------- the print service (Lob) path, which has never run live ----------
@pytest.fixture
def lob(settings):
    settings.MAIL_PROVIDER, settings.LOB_API_KEY = "lob", "test_key"
    settings.HOA_RETURN_LINE1, settings.HOA_RETURN_ZIP = "PO Box 1", "34241"
    settings.HEARING_REGULAR_COPY = False


def test_pieces_sent_through_lob_are_not_by_hand_and_carry_their_mailing_date(lob):
    n = Notice.objects.create(violation=case(), kind=K.HEARING)
    with mock.patch.object(mailing, "lob_send", return_value={"id": "ltr_1", "tracking_number": NUM_CLEAN}):
        mailing.dispatch(n, lambda who: b"%PDF-1")
    p = n.pieces.get()
    assert not p.by_hand and p.is_mailed and p.mailed_on == TODAY and not p.to_mail


def test_a_problem_found_by_tracking_is_dated_written_in_the_case_history_and_can_be_handled(lob, client):
    n = Notice.objects.create(violation=case(), kind=K.HEARING)
    with mock.patch.object(mailing, "lob_send", return_value={"id": "ltr_1", "tracking_number": NUM_CLEAN}):
        mailing.dispatch(n, lambda who: b"%PDF-1")
    bad = {"tracking_events": [{"name": "Returned to Sender", "location": "SARASOTA FL", "time": "2026-11-25T09:00:00Z"}]}
    with mock.patch.object(mailing, "lob_get", return_value=bad):
        services.refresh_mail_tracking()
    p = n.pieces.get()
    assert p.needs_attention and p.problem_on == TODAY
    assert Event.objects.filter(violation=n.violation, text__contains="could not be delivered").count() == 1
    client.force_login(staff())
    assert "Certified mail problems" in client.get("/").content.decode()
    services.record_piece_handled(p, "Owner contacted; hand delivered a copy")
    assert "Certified mail problems" not in client.get("/").content.decode()
    with mock.patch.object(mailing, "lob_get", return_value=bad):
        services.refresh_mail_tracking(force_for=n.violation)                   # asked again: no second warning, and it stays cleared
    assert Event.objects.filter(violation=n.violation, text__contains="could not be delivered").count() == 1
    assert "Certified mail problems" not in client.get("/").content.decode()


# ---------- the database change ----------
def test_the_migration_dates_the_pieces_that_were_already_with_the_print_service():
    mod = importlib.import_module("violations.migrations.0044_mail_piece_delivery_tracking")
    n = Notice.objects.create(violation=case(), kind=K.HEARING)
    old = MailPiece.objects.create(notice=n, provider_id="ltr_old", service="certified", problem=True, status="Returned")
    assert old.mailed_on is None
    mod.backfill(apps, None)
    old.refresh_from_db()
    assert old.mailed_on == old.created_at.date() and old.problem_on == old.created_at.date()


# ---------- first class mail has no delivery record; the menu counter ----------
def test_first_class_mail_has_no_delivery_record():
    n = hearing(case())
    first = pieces(n)[1]
    services.record_piece_mailed(first, TODAY, "")
    with pytest.raises(services.WorkflowError, match="Only certified"):
        services.record_piece_delivered(first, TODAY)
    with pytest.raises(services.WorkflowError, match="Only certified"):
        services.record_piece_not_delivered(first, "returned")


def test_the_case_page_offers_delivery_results_for_certified_letters_only(client):
    v = case()
    n = hearing(v)
    cert, first = pieces(n)
    services.record_piece_mailed(cert, TODAY, NUM)
    services.record_piece_mailed(first, TODAY, "")
    client.force_login(staff())
    html = client.get(f"/violations/{v.pk}/").content.decode()
    assert html.count("Record the result") == 1 and f"/mail-pieces/{cert.pk}/not_delivered/" in html and f"/mail-pieces/{first.pk}/not_delivered/" not in html


def nav_count(client):
    m = re.search(r'<small class="(?:hot)?">(\d+) to do</small>', client.get("/violations/").content.decode())
    return int(m.group(1))


def test_the_menu_counter_counts_letters_to_mail_and_undelivered_certified_letters(client):
    client.force_login(staff())
    assert nav_count(client) == 0
    n = hearing(case())
    cert, first = pieces(n)
    assert nav_count(client) == 2                                                  # two letters still to mail
    services.record_piece_mailed(cert, TODAY, NUM)
    services.record_piece_mailed(first, TODAY, "")
    assert nav_count(client) == 0
    services.record_piece_not_delivered(cert, "refused", TODAY)
    assert nav_count(client) == 1                                                  # one certified letter could not be delivered
    html = client.get("/violations/").content.decode()
    assert "1 certified letter could not be delivered" in html
    services.record_piece_handled(cert, "Owner will pick it up at the post office")
    assert nav_count(client) == 0


def test_each_letter_still_to_mail_has_a_link_that_prints_that_version(client):
    v = case(mailing_address="PO Box 5\nVenice, FL 34285", tenant=True)
    n = hearing(v)
    client.force_login(staff())
    html = client.get(f"/violations/{v.pk}/").content.decode()
    assert html.count("Print this letter") == 6
    assert f"/notices/{n.pk}/pdf/?to=tenant" in html and f"/notices/{n.pk}/pdf/?to=owner_property" in html
    assert client.get(f"/notices/{n.pk}/pdf/?to=tenant").status_code == 200
    for p in pieces(n):
        services.record_piece_mailed(p, TODAY, NUM if p.is_certified else "")
    assert "Print this letter" not in client.get(f"/violations/{v.pk}/").content.decode()


# ---------- the "check delivery" reminder for certified letters mailed by hand ----------
def test_a_certified_letter_with_no_result_after_a_week_is_listed_for_a_check(client):
    v = case()
    n = hearing(v)
    cert = pieces(n)[0]
    client.force_login(staff())
    services.record_piece_mailed(cert, TODAY - timedelta(days=6), NUM)
    assert "check delivery" not in client.get("/").content.decode().lower()          # too soon
    services.record_piece_mailed(cert, TODAY - timedelta(days=7), NUM)
    html = client.get("/").content.decode()
    assert "Certified letters: check delivery" in html and v.owner.property_address in html and "Track on USPS" in html and "Record result" in html


def test_the_check_reminder_goes_away_once_a_result_is_recorded_and_never_lists_other_letters(client):
    n = hearing(case())
    cert, first = pieces(n)
    services.record_piece_mailed(cert, TODAY - timedelta(days=10), NUM)
    services.record_piece_mailed(first, TODAY - timedelta(days=10), "")
    assert [p.pk for p in services.certified_to_check()] == [cert.pk]                # first class is never chased
    services.record_piece_delivered(cert, TODAY - timedelta(days=4))
    assert list(services.certified_to_check()) == []
    other = pieces(hearing(case()))[0]
    services.record_piece_mailed(other, TODAY - timedelta(days=10), NUM)
    services.record_piece_not_delivered(other, "refused", TODAY)
    assert list(services.certified_to_check()) == []                                  # a recorded problem has its own alert


def test_the_check_reminder_can_be_turned_off_and_counts_in_the_menu(client, settings):
    n = hearing(case())
    cert, first = pieces(n)
    services.record_piece_mailed(cert, TODAY - timedelta(days=9), NUM)
    services.record_piece_mailed(first, TODAY - timedelta(days=9), "")
    client.force_login(staff())
    assert nav_count(client) == 1
    settings.CERTIFIED_CHECK_AFTER_DAYS = 0
    assert nav_count(client) == 0 and list(services.certified_to_check()) == []


# ---------- safety of what is stored and shown ----------
def test_a_tracking_number_from_outside_cannot_break_out_of_the_usps_link():
    n = hearing(case())
    p = MailPiece.objects.create(notice=n, provider_id="ltr_x", service="certified", label="owner property", tracking_number='9407&x=1"><script>')
    assert p.usps_url == "https://tools.usps.com/go/TrackConfirmAction?tLabels=9407%26x%3D1%22%3E%3Cscript%3E"


def test_notes_that_look_like_spreadsheet_formulas_are_made_harmless_in_the_case_file():
    from violations import casefile
    n, cert = mailed_cert()
    services.record_piece_not_delivered(cert, "other", TODAY, '=HYPERLINK("http://evil.example","click")')
    data, _ = casefile.build_case_file(n.violation, staff())
    z = zipfile.ZipFile(io.BytesIO(data))
    text = z.read(next(x for x in z.namelist() if x.endswith("mailing/pieces-and-tracking.csv"))).decode("utf-8-sig")
    assert "'=HYPERLINK" in text and ',=HYPERLINK' not in text


def test_notes_and_numbers_are_shown_as_text_never_as_markup(client):
    v = case()
    n, cert = mailed_cert(v)
    services.record_piece_not_delivered(cert, "other", TODAY, "<script>alert(1)</script>")
    client.force_login(staff())
    for url in (f"/violations/{v.pk}/", "/"):
        html = client.get(url).content.decode()
        assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in client.get(f"/violations/{v.pk}/").content.decode()
