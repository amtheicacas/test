"""Tests added by the second stabilisation / security pass (Nov 2026). Each test names the problem it pins down (ids as in REVIEW_REPORT.md)."""
import pytest
from django.test import Client
from django.urls import reverse

from violations.tests.test_access_matrix import world  # noqa: F401  (the shared fixture: owners, a case, a meeting, every kind of login)

pytestmark = pytest.mark.django_db


def board_client(world, who="board"):
    c = Client(raise_request_exception=False)
    c.force_login(world["users"][who])
    return c


# ---- GET-1: a hand-edited violation-list link must give a normal page, never HTTP 500 ----------------------------------------------------------
@pytest.mark.parametrize("query", ["type=abc", "type=99999999999999999999", "type=1e999", "from=2026-13-45", "to=abc", "from=0000-00-00&to=2026-02-30", "from=a&to=b&type=c"])
def test_violation_list_ignores_a_junk_type_or_date_instead_of_crashing(world, query):
    r = board_client(world).get(reverse("violation_list") + "?" + query)
    assert r.status_code == 200
    assert "Alex" in r.content.decode()           # the junk filter is simply not applied: the case is still listed


def test_violation_list_real_filters_still_work(world):
    c = board_client(world)
    from violations.models import Violation
    v = Violation.objects.get()
    assert "Alex" in c.get(reverse("violation_list"), {"type": v.vtype_id}).content.decode()
    assert "Alex" not in c.get(reverse("violation_list"), {"type": v.vtype_id + 1}).content.decode()
    assert "Alex" not in c.get(reverse("violation_list"), {"from": "2999-01-01"}).content.decode()
    assert "Alex" in c.get(reverse("violation_list"), {"to": "2999-01-01"}).content.decode()


# ---- NUL-1: PostgreSQL cannot store a NUL (0x00) character; typing one used to be an HTTP 500 there (SQLite accepts it, which hid the problem) ------
def _case_with_a_private_link(world):
    from violations.models import Owner, Violation, ViolationType
    o = Owner.objects.get(name="Alex")
    t = ViolationType.objects.get()
    return Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c", status=Violation.Status.NOTICE_SENT)


def test_a_nul_character_in_a_posted_message_is_removed_before_anything_is_saved(world):
    v = _case_with_a_private_link(world)
    r = Client(raise_request_exception=False).post(f"/r/{v.token}/", {"message": "ab\x00cd"})
    assert r.status_code == 302
    assert [x.message for x in v.responses.all()] == ["abcd"]


def test_a_nul_character_in_a_search_box_is_removed(world):
    r = board_client(world).get("/violations/", {"q": "a\x00b"})
    assert r.status_code == 200
    assert 'value="ab"' in r.content.decode() and "\x00" not in r.content.decode()


@pytest.mark.parametrize("url", ["/r/abc%00def/", "/violations/%00/", "/m%00e/", "/accounts/link/ab%00cd/", "/%00"])
def test_a_nul_character_in_the_address_is_simply_page_not_found(world, url):
    for who in (None, "board"):
        c = board_client(world, who) if who else Client(raise_request_exception=False)
        assert c.get(url).status_code == 404


def test_the_nul_filter_cleans_get_and_post_and_leaves_clean_requests_untouched():
    from django.http import HttpResponse
    from django.test import RequestFactory

    from violations.middleware import StripNulCharacters
    mw = StripNulCharacters(lambda request: HttpResponse("ok"))
    rf = RequestFactory()
    req = rf.post("/x/?a=1&b%00=2%003&a=4", {"k\x00": ["v\x001", "v2"], "plain": "p"})
    assert mw(req).status_code == 200
    mw.process_view(req, None, (), {})
    assert dict(req.GET.lists()) == {"a": ["1", "4"], "b": ["23"]}
    assert dict(req.POST.lists()) == {"k": ["v1", "v2"], "plain": ["p"]}
    with pytest.raises(AttributeError):
        req.POST["new"] = "x"                                  # still read-only, like a normal request's form data
    clean = rf.get("/x/?a=1")
    before = clean.GET
    mw(clean)
    assert clean.GET is before                                  # nothing to remove: the very same object


# ======================================================================================================================================
# Mail dispatch safety (LB-1, LB-2, KEY-1, LB-5)
# ======================================================================================================================================
import http.client
import socket
import threading
import time
import urllib.error
from datetime import date, timedelta
from unittest import mock

from django.db import connection, connections

from violations import mailing, services
from violations.models import MailPiece, Notice, Owner, Violation, ViolationType
from violations.tests.test_certified_mail import ADDR, _FakeResponse
from violations.tests.test_tenant_mail import rented  # noqa: F401  (an owner with a mailing address, a property address and a tenant, MAIL_PROVIDER=lob)


def _fake_lob(sent):
    return lambda to, pdf, d, extra=None: sent.append((to["line1"], extra)) or {"id": f"ltr_{len(sent)}", "tracking_number": f"9407100000000000{len(sent):06d}"}


def test_LB1_one_letter_that_cannot_be_built_does_not_stop_the_others_or_lose_the_record_of_those_already_sent(rented):
    real, sent = services.render_notice_pdf, []

    def render(notice, who):
        if who == "owner_property":
            raise RuntimeError("this letter will not build")
        return real(notice, who)
    with mock.patch.object(services, "render_notice_pdf", side_effect=render), mock.patch.object(mailing, "lob_send", side_effect=_fake_lob(sent)):
        n = services.send_notice(rented, Notice.Kind.HEARING)              # (used to raise: the letters to the mailing address had already gone to Lob)
    n.refresh_from_db()
    assert [s[0] for s in sent] == ["PO Box 55"] * 2                      # certified + first class to the mailing address went
    assert MailPiece.objects.filter(notice=n).count() == 2                # ...and both are on record
    assert n.mailed_on == date.today() and "owner property" in n.provider_note and "could not be built" in n.provider_note
    assert rented.events.filter(text__contains="could not be built").exists()      # the case history says which address did not go


def test_LB1_when_every_letter_fails_to_build_nothing_is_marked_mailed_and_nothing_is_lost(rented):
    sent = []
    with mock.patch.object(services, "render_notice_pdf", side_effect=RuntimeError("no")), mock.patch.object(mailing, "lob_send", side_effect=_fake_lob(sent)):
        n = services.send_notice(rented, Notice.Kind.VIOLATION)
    n.refresh_from_db()
    assert sent == [] and n.mailed_on is None and n.provider_note.count("could not be built") == 2


def test_LB2_a_stale_second_mail_now_click_does_not_send_the_letters_again(rented):
    n = Notice.objects.create(violation=rented, kind=Notice.Kind.VIOLATION, mail_hold_until=date.today() + timedelta(days=3))
    first, second = Notice.objects.get(pk=n.pk), Notice.objects.get(pk=n.pk)      # two people's pages, both loaded before either clicked
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=_fake_lob(sent)):
        services.mail_now(first)
        count = len(sent)
        with pytest.raises(services.WorkflowError):
            services.mail_now(second)
    assert count == 2 and len(sent) == 2 and MailPiece.objects.filter(notice=n).count() == 2


@pytest.mark.django_db(transaction=True)
def test_LB2_two_page_loads_at_the_same_moment_release_a_held_notice_only_once(rented):
    """advance_expired runs on every dashboard / Board-queue page load; two loads at once used to BOTH release the same held notice."""
    if connection.vendor != "postgresql":
        pytest.skip("row locks need PostgreSQL; run with DATABASE_URL set")
    n = Notice.objects.create(violation=rented, kind=Notice.Kind.VIOLATION, mail_hold_until=date.today() - timedelta(days=1))
    sent = []

    def slow(to, pdf, d, extra=None):
        sent.append(to["line1"])
        time.sleep(0.4)
        return {"id": f"ltr_{len(sent)}"}

    def load_the_page():
        try:
            services.advance_expired(date.today())
        finally:
            connections.close_all()
    with mock.patch.object(mailing, "lob_send", side_effect=slow):
        ts = [threading.Thread(target=load_the_page) for _ in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    assert len(sent) == 2, sent                                           # the mailing address and the property, once each: never doubled
    assert MailPiece.objects.filter(notice=n).count() == 2


S_NOTICE_SENT = Violation.Status.NOTICE_SENT


def test_ADV1_advance_expired_does_not_overwrite_an_extension_made_after_it_looked(rented):
    """The list of expired cases is read once; staff can extend one of them while an earlier case is being moved. The extension must win."""
    yesterday = date.today() - timedelta(days=1)
    Violation.objects.filter(pk=rented.pk).update(status=S_NOTICE_SENT, cure_deadline=yesterday)
    twin = Violation.objects.create(owner=rented.owner, vtype=rented.vtype, description="d", cure_required="c", status=S_NOTICE_SENT, cure_deadline=yesterday)
    extended, real_log = [], services.log

    def log_then_extend_the_other_case(v, text, user=None):
        real_log(v, text, user)
        if text.startswith("Cure period ended") and not extended:
            other = twin if v.pk == rented.pk else rented
            Violation.objects.filter(pk=other.pk).update(cure_deadline=date.today() + timedelta(days=10))      # staff click 'Give more time'
            extended.append(other.pk)
    with mock.patch.object(services, "log", side_effect=log_then_extend_the_other_case):
        moved = services.advance_expired(date.today())
    kept = Violation.objects.get(pk=extended[0])
    assert moved == 1 and kept.status == S_NOTICE_SENT and kept.cure_deadline == date.today() + timedelta(days=10)
    assert Violation.objects.exclude(pk=extended[0]).get(pk__in=[rented.pk, twin.pk]).status == Violation.Status.PENDING_BOARD


def test_KEY1_a_lob_key_with_stray_spaces_is_sent_trimmed(settings):
    settings.MAIL_PROVIDER, settings.STAGING, settings.PRODUCTION, settings.LOB_API_KEY = "lob", False, False, "  test_abc123 \n"
    settings.HOA_RETURN_LINE1, settings.HOA_RETURN_CITY, settings.HOA_RETURN_STATE, settings.HOA_RETURN_ZIP = "100 Main St", "Sarasota", "FL", "34236"
    seen = {}

    def fake(req, timeout=None):
        seen["auth"] = req.get_header("Authorization")
        return _FakeResponse({"id": "ltr_1"})
    import base64

    class Ok(_FakeResponse):
        status = 200
    with mock.patch("urllib.request.urlopen", lambda req, timeout=None: (fake(req, timeout), Ok({"id": "ltr_1"}))[1]):
        mailing.lob_send(ADDR, b"%PDF-1", "Case 1")
        mailing.lob_ping()
        assert base64.b64decode(seen["auth"].split()[1]) == b"test_abc123:"
        mailing.lob_get("ltr_1")
        assert base64.b64decode(seen["auth"].split()[1]) == b"test_abc123:"


def test_LB5_a_failed_tracking_check_is_reported_not_hidden_but_never_breaks_the_job(rented, caplog):
    n = Notice.objects.create(violation=rented, kind=Notice.Kind.HEARING)
    piece = MailPiece.objects.create(notice=n, label="owner mailing", service="certified", provider_id="ltr_1", mailed_on=date.today())
    with mock.patch.object(mailing, "lob_get", side_effect=RuntimeError("Lob is down")):
        errors = []
        assert services.refresh_mail_tracking(errors=errors) == 0
        assert errors == [f"letter {piece.pk}: RuntimeError"] and f"mail piece {piece.pk} (RuntimeError)" in caplog.text
        from django.core.management import call_command
        out = __import__("io").StringIO()
        call_command("daily_jobs", stdout=out)                                      # the job still completes (a provider hiccup never breaks it)
    assert "0 certified letter(s) had tracking updates. 1 tracking check(s) could NOT be completed" in out.getvalue()


def test_ADV1_a_hold_lifted_by_someone_else_a_moment_ago_does_not_break_the_page_load(rented):
    """lift_expired_holds reads the list once; staff may lift one of the holds first. That must not raise (the dashboard runs this on every load)."""
    yesterday = date.today() - timedelta(days=1)
    Violation.objects.filter(pk=rented.pk).update(status=Violation.Status.ON_HOLD, held_from=S_NOTICE_SENT, hold_until=yesterday, hold_reason="r")
    twin = Violation.objects.create(owner=rented.owner, vtype=rented.vtype, description="d", cure_required="c", status=Violation.Status.ON_HOLD,
                                    held_from=S_NOTICE_SENT, hold_until=yesterday, hold_reason="r")
    lifted, real_log = [], services.log

    def log_then_lift_the_other_hold(v, text, user=None):
        real_log(v, text, user)
        if text.startswith("Hold ended") and not lifted:
            other = twin if v.pk == rented.pk else rented
            Violation.objects.filter(pk=other.pk).update(status=S_NOTICE_SENT, held_from="", hold_until=None)       # staff click 'Lift hold'
            lifted.append(other.pk)
    with mock.patch.object(services, "log", side_effect=log_then_lift_the_other_hold):
        assert services.lift_expired_holds(date.today()) == 1
    assert Violation.objects.get(pk=lifted[0]).status == S_NOTICE_SENT


# ======================================================================================================================================
# Reminders, re-inspection, extensions (RM-1, RM-1b, RM-3, EXT-1)
# ======================================================================================================================================
from django.core import mail

from violations.models import Reminder
from violations.tests.test_extensions import TODAY, case  # noqa: E402


def test_RM1_a_cure_reminder_that_could_not_be_emailed_is_not_marked_as_sent_and_is_tried_again(settings):
    settings.REMINDER_DAYS = (7, 2)
    v = case(deadline_in=2)
    with mock.patch("violations.services.EmailMessage.send", side_effect=OSError("mail server down")):
        assert services.send_cure_reminders() == 0
    assert not Reminder.objects.filter(violation=v).exists()                                    # (used to be recorded as sent: never retried)
    assert not v.events.filter(text__startswith="Reminder emailed").exists()
    mail.outbox.clear()
    assert services.send_cure_reminders() == 1                                                  # the mail server is back: the next daily job sends it
    assert len(mail.outbox) == 1 and Reminder.objects.filter(violation=v, sent_at__isnull=False).count() == 1
    assert v.events.filter(text__startswith="Reminder emailed").count() == 1
    assert services.send_cure_reminders() == 0                                                  # and only once


def test_RM1b_a_hold_reminder_that_could_not_be_emailed_is_not_marked_as_sent_and_is_tried_again(settings):
    settings.REMINDER_DAYS = (7, 2)
    v = case(deadline_in=30, status=Violation.Status.ON_HOLD, hold_until=TODAY + timedelta(days=5))
    with mock.patch("violations.services.EmailMessage.send", side_effect=OSError("mail server down")):
        assert services.send_hold_reminders() == 0
    assert not Reminder.objects.filter(violation=v).exists()
    assert not v.events.filter(text__startswith="Hold-ending reminder emailed").exists()
    mail.outbox.clear()
    assert services.send_hold_reminders() == 1 and len(mail.outbox) == 1
    assert Reminder.objects.filter(violation=v, sent_at__isnull=False).count() == 1
    assert services.send_hold_reminders() == 0


def test_RM1b_a_hold_with_nobody_to_email_is_recorded_as_skipped_not_as_sent(settings):
    settings.REMINDER_DAYS = (7, 2)
    v = case(deadline_in=30, status=Violation.Status.ON_HOLD, hold_until=TODAY + timedelta(days=5), email="")
    assert services.send_hold_reminders() == 0
    assert Reminder.objects.filter(violation=v).count() == 1 and Reminder.objects.filter(violation=v, sent_at__isnull=False).count() == 0


def test_RM3_a_double_click_on_reinspect_does_not_thank_the_owner_twice():
    v = case(deadline_in=5)
    first, second = Violation.objects.get(pk=v.pk), Violation.objects.get(pk=v.pk)           # two pages loaded before either click
    services.mark_reinspect(first)
    with pytest.raises(services.WorkflowError):
        services.mark_reinspect(second)
    assert v.responses.filter(from_resident=False, message__startswith="Thank you. Your reply has been received").count() == 1
    assert v.events.filter(text__startswith="Marked for re-inspection").count() == 1


def test_RM3_a_double_click_on_the_reinspection_result_does_not_message_the_owner_twice():
    v = case(deadline_in=5)
    services.mark_reinspect(v, notify=False)
    first, second = Violation.objects.get(pk=v.pk), Violation.objects.get(pk=v.pk)
    services.reinspect_result(first, corrected=False, note="Still there")
    with pytest.raises(services.WorkflowError):
        services.reinspect_result(second, corrected=False, note="Still there")
    assert v.responses.filter(message__startswith="We re-inspected").count() == 1
    assert v.events.filter(text__startswith="Re-inspected: still not corrected").count() == 1


def test_EXT1_give_more_time_cannot_shorten_the_cure_period():
    v = case(deadline_in=10)
    for bad in (TODAY + timedelta(days=3), TODAY + timedelta(days=10)):                       # earlier than, or equal to, the current deadline
        with pytest.raises(services.WorkflowError, match="later than the current deadline"):
            services.grant_extension(v, new_deadline=bad)
    v.refresh_from_db()
    assert v.cure_deadline == TODAY + timedelta(days=10) and v.extended_days == 0 and mail.outbox == []
    services.grant_extension(v, new_deadline=TODAY + timedelta(days=11))                      # a later date still works
    v.refresh_from_db()
    assert v.cure_deadline == TODAY + timedelta(days=11) and v.extended_days == 1


def test_EXT1_a_second_notice_may_still_reset_the_deadline_to_any_future_date():
    v = case(deadline_in=10)
    services.grant_extension(v, new_deadline=TODAY + timedelta(days=4), second_notice=True)
    v.refresh_from_db()
    assert v.cure_deadline == TODAY + timedelta(days=4)


# ======================================================================================================================================
# Staff input that is not a number / not one of ours (RM-2, NUM-1, NUM-2, CSV-1)
# ======================================================================================================================================
from violations.tests.test_history_nav import _meeting_with  # noqa: E402


@pytest.mark.parametrize("do", ["download", "send"])
def test_RM2_tampered_attachment_keys_on_the_attorney_email_are_ignored_not_a_server_error(world, do):
    Violation.objects.filter(pk=world["ids"]["violation"]).update(status=Violation.Status.REFERRED_LEGAL)
    r = board_client(world).post(reverse("legal_email", args=[world["ids"]["violation"]]),
                                 {"do": do, "to": "lawyer@example.com", "subject": "s", "body": "b",
                                  "att": ["letter-abc", "photo-", "letter-99999999999999999999999", "x", "letter--1", "photo-1e5", "letter-", "photo-٣"]})
    assert r.status_code in (200, 302), r.status_code


def test_RM2_real_attachment_keys_still_work(world):
    vid = world["ids"]["violation"]
    Violation.objects.filter(pk=vid).update(status=Violation.Status.REFERRED_LEGAL)
    draft = services.legal_email_draft(Violation.objects.get(pk=vid))
    keys = [a["key"] for a in draft["attachments"]]
    assert any(k.startswith("letter-") for k in keys) and any(k.startswith("photo-") for k in keys)
    r = board_client(world).post(reverse("legal_email", args=[vid]), {"do": "download", "to": "l@example.com", "subject": "s", "body": "b", "att": keys})
    assert r.status_code == 200 and r["Content-Type"] == "application/zip"
    import io, zipfile
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "email-draft.txt" in names and len(names) == 1 + len(keys)


@pytest.mark.parametrize("votes", [(4, -1), (-1, 4), (3, 200), (500, 0), (0, -3)])
def test_NUM1_negative_or_absurd_vote_counts_are_refused(django_user_model, votes):
    m, (a,), cu = _meeting_with(1, django_user_model)
    with pytest.raises(services.WorkflowError, match="whole numbers"):
        services.committee_record(a, "confirm", cu, votes_for=votes[0], votes_against=votes[1], findings="ok")
    a.refresh_from_db()
    assert a.committee_outcome == "" and a.committee_votes_for is None
    services.committee_record(a, "confirm", cu, votes_for=3, votes_against=0, findings="ok")          # a normal vote still works


def test_NUM1_the_older_vote_service_refuses_negative_counts_too(django_user_model):
    m, (a,), cu = _meeting_with(1, django_user_model)
    with pytest.raises(services.WorkflowError, match="whole numbers"):
        services.committee_decide(a, 4, -1, cu)
    a.refresh_from_db()
    assert a.status == Violation.Status.HEARING_SCHEDULED


def test_NUM1_through_the_page_a_vote_of_4_to_minus_1_records_nothing(django_user_model):
    m, (a,), cu = _meeting_with(1, django_user_model)
    c = Client(raise_request_exception=False)
    c.force_login(cu)
    r = c.post(f"/committee/cases/{a.pk}/record/", {"outcome": "confirm", "votes_for": "4", "votes_against": "-1", "findings": "x"})
    assert r.status_code == 302
    a.refresh_from_db()
    assert a.committee_outcome == "" and a.committee_votes_for is None


@pytest.mark.parametrize("bad", ["abc", "NaN", "-NaN", "sNaN", "Infinity", "-Infinity", "1e999", "12.345", "99999999", "1,000", "$100", "--5"])
def test_NUM2_an_amount_that_is_not_money_is_a_plain_message_never_a_server_error(django_user_model, bad):
    m, (a,), cu = _meeting_with(1, django_user_model)
    with pytest.raises(services.WorkflowError):
        services.committee_record(a, "amend", cu, amount=bad, votes_for=3, votes_against=0, findings="x")
    c = Client(raise_request_exception=False)
    c.force_login(cu)
    assert c.post(f"/committee/cases/{a.pk}/record/", {"outcome": "amend", "amount": bad, "votes_for": "3", "votes_against": "0", "findings": "x"}).status_code == 302
    a.refresh_from_db()
    assert a.committee_outcome == "" and a.committee_amended_fine is None


def test_NUM2_a_Board_modify_with_a_bad_amount_is_a_plain_message(django_user_model):
    m, (a,), cu = _meeting_with(1, django_user_model)
    services.committee_record(a, "confirm", cu, votes_for=3, votes_against=0, findings="ok")
    services.submit_meeting_results(m, cu)
    a.refresh_from_db()
    for bad in ("abc", "NaN", "12.345", "1e999"):
        with pytest.raises(services.WorkflowError):
            services.board_review_item(a, cu, "modify", amount=bad)
    services.board_review_item(a, cu, "modify", amount="40")                                         # a proper amount still works
    a.refresh_from_db()
    assert a.fine_amount == 40


def test_NUM2_proper_amounts_still_work(django_user_model):
    m, (a,), cu = _meeting_with(1, django_user_model)
    services.committee_record(a, "amend", cu, amount="60.50", votes_for=3, votes_against=0, findings="x")
    a.refresh_from_db()
    assert str(a.committee_amended_fine) == "60.50"


@pytest.mark.parametrize("text,safe", [("- 1+cmd|' /C calc'!A0", "'- 1+cmd|' /C calc'!A0"), ("- gate was open", "'- gate was open"), ("  =1+1", "'  =1+1"),
                                       ("@SUM(1)", "'@SUM(1)"), ("\n=1+1", "'\n=1+1"), ("+1", "'+1"), ("=1", "'=1"), ("\t=1", "'\t=1"),
                                       ("Gate was open", "Gate was open"), ("Re: - note", "Re: - note"), ("   ", "   "), ("", "")])
def test_CSV1_a_cell_that_could_run_as_a_formula_is_defused(text, safe):
    from violations import casefile
    assert casefile._safe_cell(text) == safe


# ======================================================================================================================================
# Recording what happened to each mailed letter (PIECE-1)
# ======================================================================================================================================
NUM = "94071000000000000000"


def _mailed_piece(world):
    piece = MailPiece.objects.get()
    services.record_piece_mailed(piece, date.today() - timedelta(days=3), NUM)
    return piece


def test_PIECE1_a_stale_page_cannot_overwrite_a_delivery_that_was_just_recorded(world):
    piece = _mailed_piece(world)
    first, stale = MailPiece.objects.get(pk=piece.pk), MailPiece.objects.get(pk=piece.pk)
    services.record_piece_delivered(first, date.today() - timedelta(days=1))
    with pytest.raises(services.WorkflowError, match="recorded as delivered"):
        services.record_piece_not_delivered(stale, "refused")                    # (used to pass: the stale copy still said "not delivered")
    piece.refresh_from_db()
    assert piece.delivered_on and not piece.problem and piece.status == "Delivered"


def test_PIECE1_a_stale_page_cannot_clear_a_problem_that_was_already_cleared_nor_report_it_twice(world):
    piece = _mailed_piece(world)
    services.record_piece_not_delivered(MailPiece.objects.get(pk=piece.pk), "refused")
    a, b = MailPiece.objects.get(pk=piece.pk), MailPiece.objects.get(pk=piece.pk)
    services.record_piece_handled(a, "Called the owner")
    with pytest.raises(services.WorkflowError, match="nothing to clear"):
        services.record_piece_handled(b, "Called the owner again")
    assert Violation.objects.get().events.filter(text__contains="marked handled").count() == 1


def test_PIECE1_an_error_half_way_leaves_the_letter_unchanged(world):
    piece = MailPiece.objects.get()
    with mock.patch.object(services, "_sync_notice_from_pieces", side_effect=RuntimeError("database hiccup")):
        with pytest.raises(RuntimeError):
            services.record_piece_mailed(piece, date.today() - timedelta(days=3), NUM)
    fresh = MailPiece.objects.get(pk=piece.pk)
    assert fresh.mailed_on is None and fresh.tracking_number == ""                # all or nothing: not half recorded
    assert not Violation.objects.get().events.filter(text__contains="mailed").exists()


@pytest.mark.django_db(transaction=True)
def test_PIECE1_two_people_recording_the_two_letters_of_one_notice_at_once_still_mark_the_notice_mailed(world):
    if connection.vendor != "postgresql":
        pytest.skip("row locks need PostgreSQL; run with DATABASE_URL set")
    n = Notice.objects.get()
    other = MailPiece.objects.create(notice=n, label="owner property", service="first_class", by_hand=True, status="To be mailed")
    first = MailPiece.objects.exclude(pk=other.pk).get()
    real = services._sync_notice_from_pieces

    def slow_sync(notice):
        time.sleep(0.4)                    # long enough for the other person's save to happen before this one works out the notice
        return real(notice)

    def record(piece):
        try:
            services.record_piece_mailed(MailPiece.objects.get(pk=piece.pk), date.today(), NUM if piece.service == "certified" else "")
        finally:
            connections.close_all()
    with mock.patch.object(services, "_sync_notice_from_pieces", side_effect=slow_sync):
        ts = [threading.Thread(target=record, args=(p,)) for p in (first, other)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    n.refresh_from_db()
    assert n.mailed_on == date.today() and n.tracking_number == NUM


# ======================================================================================================================================
# Submitting a meeting's results twice (RM-5) and the ledger message after a time-out (LEDGER-1)
# ======================================================================================================================================
@pytest.mark.django_db(transaction=True)
def test_RM5_two_people_submitting_a_meetings_results_at_once_submit_them_only_once(django_user_model):
    if connection.vendor != "postgresql":
        pytest.skip("row locks need PostgreSQL; run with DATABASE_URL set")
    m, (a,), cu = _meeting_with(1, django_user_model)
    services.committee_record(a, "confirm", cu, votes_for=3, votes_against=0, findings="ok")
    real_log, results = services.log, []

    def slow_log(v, text, user=None):
        real_log(v, text, user)
        time.sleep(0.4)

    def submit():
        try:
            from violations.models import CommitteeMeeting
            services.submit_meeting_results(CommitteeMeeting.objects.get(pk=m.pk), cu)          # each person's page loaded the meeting before either clicked
            results.append("ok")
        except services.WorkflowError:
            results.append("refused")
        finally:
            connections.close_all()
    with mock.patch.object(services, "log", side_effect=slow_log):
        ts = [threading.Thread(target=submit) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    assert sorted(results) == ["ok", "refused"], results
    assert Violation.objects.get(pk=a.pk).events.filter(text__startswith="Committee results submitted").count() == 1


def test_RM5_a_stale_second_submit_is_refused(django_user_model):
    from violations.models import CommitteeMeeting
    m, (a,), cu = _meeting_with(1, django_user_model)
    services.committee_record(a, "confirm", cu, votes_for=3, votes_against=0, findings="ok")
    first, stale = CommitteeMeeting.objects.get(pk=m.pk), CommitteeMeeting.objects.get(pk=m.pk)
    services.submit_meeting_results(first, cu)
    with pytest.raises(services.WorkflowError, match="already submitted"):
        services.submit_meeting_results(stale, cu)


@pytest.mark.parametrize("error,says_maybe", [
    (TimeoutError("timed out"), True), (ConnectionResetError("reset"), True), (ValueError("not JSON"), True), (http.client.RemoteDisconnected("closed"), True),
    (urllib.error.HTTPError("https://v", 502, "Bad Gateway", {}, None), True), (urllib.error.URLError(TimeoutError("timed out")), True),
    (urllib.error.HTTPError("https://v", 422, "Unprocessable", {}, None), False), (urllib.error.URLError(ConnectionRefusedError("refused")), False),
    (urllib.error.URLError(socket.gaierror("no such host")), False), (RuntimeError("Vantaca API is not configured"), False)])
def test_LEDGER1_after_a_time_out_the_message_does_not_claim_that_nothing_was_posted(settings, error, says_maybe):
    from violations import ledger, vantaca
    settings.VANTACA_LEDGER_POST_PATH, settings.VANTACA_FINE_CHARGE_CODE, settings.VANTACA_BASE_URL = "/ledger", "FINE", "https://vantaca.example"
    o = Owner.objects.create(name="Pat", property_address="1 A St", account_number="A1")
    v = Violation.objects.create(owner=o, vtype=ViolationType.objects.create(name="T3"), description="d", cure_required="c", fine_amount=100)
    with mock.patch.object(vantaca, "api_request", side_effect=error):
        with pytest.raises(ledger.LedgerError) as e:
            ledger.post_fine(v, date.today())
    text = str(e.value)
    assert ("NOT certain" in text and f"HOA-VIOL-{v.pk}" in text and "Nothing was posted" not in text) == says_maybe
    assert ("Nothing was posted" in text) != says_maybe


# ======================================================================================================================================
# The private owner link shows a case without a login (PRIV-1)
# ======================================================================================================================================
def test_PRIV1_the_private_link_page_is_never_kept_by_the_browser(world):
    v = _case_with_a_private_link(world)
    for path in (f"/r/{v.token}/",):
        r = Client().get(path)
        assert r.status_code == 200 and "no-store" in r["Cache-Control"] and r["Referrer-Policy"] == "no-referrer"
    r = Client().post(f"/r/{v.token}/", {"message": ""})                   # the page re-shown with a form error is private too
    assert "no-store" in r["Cache-Control"]


# ======================================================================================================================================
# Pages must not run a few more database queries for every row (PERF-1): the Board queue and the dashboard on a big backlog
# ======================================================================================================================================
from django.db import connection as _conn
from django.test.utils import CaptureQueriesContext


def _queries(client, url):
    with CaptureQueriesContext(_conn) as ctx:
        r = client.get(url)
    assert r.status_code == 200
    return len(ctx)


_made = [0]


def _backlog(n, status):
    from violations.models import Photo, Response
    t = ViolationType.objects.get_or_create(name=f"Backlog type {status}")[0]
    for _ in range(n):
        _made[0] += 1
        k = _made[0]
        v = Violation.objects.create(owner=Owner.objects.create(name=f"B{k} Family", property_address=f"{k} {status} Way"), vtype=t, description="d",
                                     cure_required="c", status=status, fine_amount=100,
                                     cure_deadline=date.today() + timedelta(days=10 if status == Violation.Status.NOTICE_SENT else -3))
        Notice.objects.create(violation=v, kind="violation")
        Response.objects.create(violation=v, message="hello", from_resident=True, extension_requested=bool(k % 2))
        Photo.objects.create(violation=v, image="photos/none.jpg")


@pytest.mark.parametrize("status,url", [(Violation.Status.PENDING_BOARD, "/board/"), (Violation.Status.FINE_APPROVED, "/"), (Violation.Status.HEARING_NEEDED, "/meetings/"),
                                        (Violation.Status.NOTICE_SENT, "/"), (Violation.Status.LEDGER_PENDING, "/")])
def test_PERF1_a_longer_list_does_not_mean_more_queries_per_row(world, status, url):
    c = board_client(world)
    _backlog(2, status)
    small = _queries(c, url)
    _backlog(8, status)
    big = _queries(c, url)
    assert big - small <= 4, f"{url}: {small} queries for 2 extra cases, {big} for 10 (about {(big - small) / 8:.1f} more per case)"


# ======================================================================================================================================
# NEXT-1: a hand-edited "go back to" value that is not a page on this site must be ignored, not crash
# ======================================================================================================================================
@pytest.mark.parametrize("bad_next", ["0", "abc", "evil", "javascript:alert(1)", "//evil.example/x", "/\\evil.example", "https://evil.example/", "\t//evil.example", "..", " /ok"])
def test_NEXT1_a_bad_next_value_after_a_staff_action_goes_to_the_case_not_to_an_error_or_another_site(world, bad_next):
    vid = world["ids"]["violation"]
    r = board_client(world).post(reverse("violation_action", args=[vid, "ext_decline"]), {"response_id": "999", "next": bad_next})
    assert r.status_code == 302, r.status_code
    assert r["Location"].startswith("/") and not r["Location"].startswith("//") and "evil" not in r["Location"]


def test_NEXT1_a_proper_next_value_is_still_followed(world):
    vid = world["ids"]["violation"]
    r = board_client(world).post(reverse("violation_action", args=[vid, "ext_decline"]), {"response_id": "999", "next": "/board/?x=1"})
    assert r.status_code == 302 and r["Location"] == "/board/?x=1"


@pytest.mark.parametrize("bad_next", ["abc", "0", "..", "//evil.example/", "https://evil.example/", "javascript:alert(1)"])
def test_NEXT1_a_bad_next_value_on_the_two_step_page_goes_home_not_to_an_error_page(client, settings, bad_next):
    from django_otp.plugins.otp_totp.models import TOTPDevice
    from violations.tests.test_security import PW, code, mk_user
    settings.MFA_REQUIRED_ROLES = ("manager",)
    mk_user("manager")
    client.post("/accounts/login/", {"username": "manager", "password": PW})
    client.get("/security/setup/")
    dev = TOTPDevice.objects.get()
    r = client.post("/security/setup/", {"code": code(dev), "next": bad_next})
    assert r.status_code == 302 and r.url == "/"


def test_NEXT2_the_page_you_wanted_survives_the_two_step_sign_in_with_its_own_search_terms(client, settings):
    from django_otp.plugins.otp_totp.models import TOTPDevice
    from violations.tests.test_security import PW, code, mk_user
    settings.MFA_REQUIRED_ROLES = ("manager",)
    mk_user("manager")
    client.post("/accounts/login/", {"username": "manager", "password": PW})
    client.get("/security/setup/")
    dev = TOTPDevice.objects.get()
    client.post("/security/setup/", {"code": code(dev)})
    client.logout()
    client.post("/accounts/login/", {"username": "manager", "password": PW})
    r = client.get("/violations/?status=open&q=a+b")
    assert r.status_code == 302 and r.url.startswith("/security/verify/")
    TOTPDevice.objects.update(last_t=-1, throttling_failure_count=0, throttling_failure_timestamp=None)
    r = client.post(r.url, {"code": code(TOTPDevice.objects.get())})
    assert r.status_code == 302 and r.url == "/violations/?status=open&q=a+b"


# ======================================================================================================================================
# Moving a meeting: every case on it moves together or not at all (MTG-1)
# ======================================================================================================================================
def test_MTG1_moving_a_meeting_keeps_the_meeting_and_all_its_cases_in_step_even_if_a_notice_fails(django_user_model):
    from django.utils import timezone as tz
    from violations.models import CommitteeMeeting
    m, cases, cu = _meeting_with(2, django_user_model)
    new_start = tz.now() + timedelta(days=40)
    real, calls = services.send_notice, []

    def flaky(v, kind, user=None, hold_mail_days=0):
        calls.append(v.pk)
        if len(calls) == 1:
            raise RuntimeError("storage hiccup")           # the first owner's notice blows up
        return real(v, kind, user, hold_mail_days)
    with mock.patch.object(services, "send_notice", side_effect=flaky):
        warnings = services.reschedule_meeting(m, new_start, cu, notify=True)
    assert CommitteeMeeting.objects.get(pk=m.pk).starts_at == new_start
    assert {Violation.objects.get(pk=c.pk).hearing_date for c in cases} == {new_start}          # (used to stop half way: the second case kept the old date)
    assert len(calls) == 2 and any("could not be sent" in w for w in warnings)


def test_MTG1_a_failure_while_saving_the_cases_moves_nothing(django_user_model):
    from django.utils import timezone as tz
    from violations.models import CommitteeMeeting
    m, cases, cu = _meeting_with(2, django_user_model)
    old = CommitteeMeeting.objects.get(pk=m.pk).starts_at
    real_log, n = services.log, []

    def log_then_fail(v, text, user=None):
        n.append(1)
        if len(n) == 2:
            raise RuntimeError("database hiccup")
        return real_log(v, text, user)
    with mock.patch.object(services, "log", side_effect=log_then_fail):
        with pytest.raises(RuntimeError):
            services.reschedule_meeting(m, tz.now() + timedelta(days=40), cu, notify=False)
    assert CommitteeMeeting.objects.get(pk=m.pk).starts_at == old
    assert {Violation.objects.get(pk=c.pk).hearing_date for c in cases} == {old}


@pytest.mark.parametrize("value", ["/\x1f", "/\x00x", "/a\x7fb", "/\t/evil.example", "/ok\x0bx", "\x1f/ok"])
def test_NEXT1_an_address_with_a_control_character_in_it_is_never_followed(value):
    from violations import accounts
    assert accounts.local_path(value, "testserver") == ""
    assert accounts.local_path("/ok/?a=1&b=2", "testserver") == "/ok/?a=1&b=2"
