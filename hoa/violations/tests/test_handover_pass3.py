"""The safer hand-over of letters to the print service (Lob), built after the review (2026-11-23).

On the live site (MAIL_BACKGROUND on) a notice that must go by mail is QUEUED when the page's work is saved and handed to Lob just after the page has
answered, by a background thread; the daily job picks up anything still queued and the dashboard flags anything stuck. Each letter is recorded the moment
Lob accepts it (no transaction around the Lob calls), a hand-over that stopped half way is resumed without sending a letter twice, and a claim keeps two
people (or a page and the daily job) from handing over the same notice at once. In development and in the tests (MAIL_BACKGROUND off) everything is
handed over at once, exactly as before.
"""
import threading
import time
from datetime import date, timedelta
from unittest import mock

import pytest
from django.core.management import call_command
from django.db import connection, connections
from django.utils import timezone

from violations import mailing, services
from violations.models import CommitteeMeeting, MailPiece, Notice, Owner, SentLetter, Violation, ViolationType
from violations.tests.test_decisions_pass3 import ppl, web  # noqa: F401
from violations.tests.test_tenant_mail import rented  # noqa: F401  (an owner with a mailing address, a property address and a tenant; Lob on)

S = Violation.Status
TODAY = date.today()
pytestmark = pytest.mark.django_db


class Boom(BaseException):
    """What a killed process looks like to the code: it is not an Exception, so nothing in the hand-over may swallow it."""


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Nothing in this file may ever reach the print service (or anything else) for real: an unmocked call fails loudly."""
    def refuse(*a, **kw):
        raise AssertionError("a test tried to reach the network")
    monkeypatch.setattr("urllib.request.urlopen", refuse)


@pytest.fixture
def deferred(settings):
    settings.MAIL_BACKGROUND = True
    settings.MAIL_LEASE_MINUTES = 15
    return settings


def lob(sent, refuse=(), boom_on=None):
    """A Lob that works (and remembers each letter), refuses some street addresses, or dies on the Nth call."""
    def send(to, pdf, description, extra=None):
        if boom_on and len(sent) + 1 == boom_on:
            raise Boom()
        if to["line1"] in refuse:
            raise RuntimeError("Lob said: address is undeliverable")
        sent.append((to["line1"], extra))
        return {"id": f"ltr_{len(sent)}", "tracking_number": f"9407100000000000{len(sent):06d}" if extra else ""}
    return send


def queued(rented, user=None):
    """A hearing notice for the `rented` case, queued the way the live site queues it (nothing sent yet)."""
    return services.send_notice(rented, Notice.Kind.HEARING, user)


def lapse(n, minutes=20):
    Notice.objects.filter(pk=n.pk).update(mail_claimed_at=timezone.now() - timedelta(minutes=minutes))


def fresh(n):
    return Notice.objects.get(pk=n.pk)


# ======================================================================================================================================
# the request only queues; the hand-over happens afterwards
# ======================================================================================================================================
def test_HO1_in_background_mode_the_request_only_queues_the_notice_and_asks_for_a_hand_over(rented, deferred, ppl, django_capture_on_commit_callbacks):
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent)), django_capture_on_commit_callbacks(execute=False) as callbacks:
        n = queued(rented, ppl.ann)
    assert sent == [] and len(callbacks) == 1                                                  # nothing went to Lob; the hand-over was asked for once
    n = fresh(n)
    assert n.mail_queued and n.mail_queued_on and n.mailed_on is None and n.mail_queued_by == ppl.ann and n.mail_waiting and not n.mail_held
    assert not MailPiece.objects.filter(notice=n).exists()
    assert n.emailed_at is not None                                                            # (the email still goes at once)


def test_HO1_the_hand_over_sends_every_letter_records_each_and_clears_the_queue(rented, deferred, ppl):
    n = queued(rented, ppl.ann)
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent)):
        assert services.hand_over_queued() == 1
        assert services.hand_over_queued() == 0                                                # nothing left to do the second time
    n = fresh(n)
    assert sorted(s[0] for s in sent) == ["12 Palm Way"] * 2 + ["PO Box 55"] * 2               # mailing + property address, certified + first class each
    assert n.mailed_on == TODAY and n.mail_queued_on is None and n.mail_claimed_at is None and n.provider == "lob" and len(n.provider_ids.split(",")) == 4
    assert MailPiece.objects.filter(notice=n).count() == 4 and not n.mail_waiting
    copies = SentLetter.objects.filter(notice=n).exclude(channel="email")
    assert copies.count() == 4 and {c.sent_by_id for c in copies} == {ppl.ann.pk}              # the saved copies still say WHO asked for the letters


def test_HO1_without_the_background_setting_everything_is_handed_over_at_once_as_before(rented, settings):
    settings.MAIL_BACKGROUND = False
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent)):
        n = queued(rented)
    n = fresh(n)
    assert len(sent) == 4 and n.mailed_on == TODAY and n.mail_queued_on is None and n.mail_claimed_at is None


def test_HO1_with_no_print_service_nothing_is_queued(rented, deferred, settings):
    settings.MAIL_PROVIDER = "manual"
    n = queued(rented)
    n = fresh(n)
    assert n.mail_queued_on is None and MailPiece.objects.filter(notice=n, by_hand=True).exists()      # the by-hand list is made at once, as before


def test_HO2_assigning_a_case_to_a_meeting_never_calls_the_print_service_and_the_case_is_saved_first(rented, deferred, ppl):
    Violation.objects.filter(pk=rented.pk).update(status=S.HEARING_NEEDED)
    rented.refresh_from_db()
    meeting = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=30), zoom_link="https://z.example/9")
    with mock.patch.object(mailing, "lob_send", side_effect=AssertionError("the request must not call Lob")):
        services.assign_to_meeting(rented, meeting, ppl.ann)
    rented.refresh_from_db()
    assert rented.status == S.HEARING_SCHEDULED and rented.committee_meeting_id == meeting.pk
    n = Notice.objects.get(violation=rented, kind=Notice.Kind.HEARING)
    assert n.mail_queued


def test_HO2_a_whole_batch_is_assigned_without_waiting_for_the_print_service_and_then_sent_once_each(rented, deferred, ppl):
    meeting = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=30), zoom_link="https://z.example/9")
    cases = [rented]
    for i in range(5):
        o = Owner.objects.create(name=f"Pat {i}", property_address=f"{20 + i} Batch Way", city="Sarasota", state="FL", zip_code="34240", email=f"p{i}@example.com",
                                 mailing_address=f"PO Box {70 + i}, Tampa, FL 33601", mail_line1=f"PO Box {70 + i}", mail_city="Tampa", mail_state="FL", mail_zip="33601")
        cases.append(Violation.objects.create(owner=o, vtype=rented.vtype, description="d", cure_required="c", status=S.HEARING_NEEDED, proposed_fine=100,
                                              board_meeting_date=TODAY))
    Violation.objects.filter(pk=rented.pk).update(status=S.HEARING_NEEDED)
    started = time.monotonic()
    with mock.patch.object(mailing, "lob_send", side_effect=AssertionError("the request must not call Lob")):
        done, skipped = services.assign_all_to_meeting(meeting, ppl.ann, cases=list(Violation.objects.filter(pk__in=[c.pk for c in cases])))
    assert len(done) == 6 and skipped == [] and time.monotonic() - started < 30
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent)):
        assert services.hand_over_queued() == 6
    assert len(sent) == 24 and len(set(sent)) == 24                                              # every letter once (4 each for 6 different homes)
    assert Notice.objects.filter(mailed_on=TODAY, kind=Notice.Kind.HEARING).count() == 6


def test_HO2_a_print_service_that_dies_half_way_leaves_the_case_saved_and_every_accepted_letter_recorded(rented, deferred, ppl):
    Violation.objects.filter(pk=rented.pk).update(status=S.HEARING_NEEDED)
    rented.refresh_from_db()
    meeting = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=30), zoom_link="https://z.example/9")
    services.assign_to_meeting(rented, meeting, ppl.ann)
    n = Notice.objects.get(violation=rented, kind=Notice.Kind.HEARING)
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent, boom_on=3)), pytest.raises(Boom):
        services.hand_over_queued()                                                            # the process is "killed" at the third letter
    rented.refresh_from_db()
    assert rented.status == S.HEARING_SCHEDULED                                                # the case was saved BEFORE any letter went
    assert len(sent) == 2 and MailPiece.objects.filter(notice=n).exclude(status=MailPiece.NOT_SENT).count() == 2      # both accepted letters are on record
    n = fresh(n)
    assert n.mail_queued and n.mail_claimed_at is not None and n.mailed_on is None            # still queued, and marked as "someone was working on it"


def test_HO3_a_hand_over_that_stopped_half_way_is_resumed_and_no_letter_goes_twice(rented, deferred, ppl):
    n = queued(rented, ppl.ann)
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent, boom_on=3)), pytest.raises(Boom):
        services.hand_over_queued()
    first_two = list(sent)
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent)):
        assert services.hand_over_queued() == 0                                                # the claim is still fresh: the "other" worker may be alive
        lapse(n)
        assert services.hand_over_queued() == 1                                                # it lapsed: pick it up where it stopped
    assert len(sent) == 4 and len(set(sent)) == 4                                              # four different letters, each exactly once
    assert sent[:2] == first_two
    n = fresh(n)
    assert n.mailed_on == TODAY and n.mail_queued_on is None and MailPiece.objects.filter(notice=n).count() == 4 and len(n.provider_ids.split(",")) == 4


def test_HO3_handing_the_same_notice_over_twice_by_hand_sends_nothing_the_second_time(rented, settings):
    settings.MAIL_BACKGROUND = False
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent)):
        n = queued(rented)
        assert mailing.dispatch(fresh(n), lambda who: b"%PDF-1.4") is False                    # every letter is already on record
    assert len(sent) == 4 and mailing.letters_outstanding(fresh(n)) == []


# ======================================================================================================================================
# the claim
# ======================================================================================================================================
def test_HO4_while_another_hand_over_is_in_progress_nobody_else_touches_the_notice(rented, deferred, ppl):
    n = queued(rented, ppl.ann)
    Notice.objects.filter(pk=n.pk).update(mail_claimed_at=timezone.now())                      # a hand-over started a moment ago
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent)):
        assert services.hand_over_queued() == 0 and services.send_due_mailings() == 0
        with pytest.raises(services.WorkflowError, match="being sent right now"):
            services.mail_now(n, ppl.ann)
        lapse(n)
        services.mail_now(n, ppl.ann)
    assert len(sent) == 4


@pytest.mark.django_db(transaction=True)
def test_HO4_three_hand_overs_at_the_same_moment_send_each_letter_once(rented, deferred, monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("row-level concurrency needs PostgreSQL; run with DATABASE_URL set")
    monkeypatch.setattr(services, "_kick", lambda: None)                                       # (this test starts its own three threads; no automatic one)
    n = queued(rented)
    sent = []

    def slow(to, pdf, d, extra=None):
        sent.append((to["line1"], extra))
        time.sleep(0.3)
        return {"id": f"ltr_{len(sent)}", "tracking_number": "9407100000000000000001" if extra else ""}

    def run():
        try:
            services.hand_over_queued()
        finally:
            connections.close_all()
    with mock.patch.object(mailing, "lob_send", side_effect=slow):
        ts = [threading.Thread(target=run) for _ in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    assert len(sent) == 4 and len(set(sent)) == 4
    assert MailPiece.objects.filter(notice=n).count() == 4


# ======================================================================================================================================
# failures
# ======================================================================================================================================
def test_HO5_when_every_letter_fails_the_notice_stays_queued_and_a_later_try_sends_it(rented, deferred, ppl):
    n = queued(rented, ppl.ann)
    with mock.patch.object(mailing, "lob_send", side_effect=RuntimeError("Lob is down")):
        assert services.hand_over_queued() == 1
    n = fresh(n)
    assert n.mailed_on is None and n.mail_queued and n.mail_claimed_at is not None            # still waiting; "tried a moment ago"
    assert MailPiece.objects.filter(notice=n, status=MailPiece.NOT_SENT, handled_on__isnull=True).count() == 4
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent)):
        assert services.hand_over_queued() == 0                                                # too soon after the failed try
        lapse(n)
        assert services.hand_over_queued() == 1
    n = fresh(n)
    assert len(sent) == 4 and n.mailed_on == TODAY and n.mail_queued_on is None
    assert not MailPiece.objects.filter(notice=n, status=MailPiece.NOT_SENT, handled_on__isnull=True).exists()      # the red-box entries closed themselves


def test_HO5_when_only_some_letters_fail_the_notice_is_done_and_the_failures_wait_for_a_person(rented, deferred, ppl):
    n = queued(rented, ppl.ann)
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent, refuse=("12 Palm Way",))):
        services.hand_over_queued()
        lapse(n)
        services.hand_over_queued()
        services.send_due_mailings()
    n = fresh(n)
    assert len(sent) == 2 and n.mailed_on == TODAY and n.mail_queued_on is None            # (no automatic second try of a letter that was refused)
    assert MailPiece.objects.filter(notice=n, status=MailPiece.NOT_SENT, handled_on__isnull=True).count() == 2


def test_HO5_a_person_can_try_the_failed_letters_again_with_mail_now(rented, deferred, ppl):
    n = queued(rented, ppl.ann)
    with mock.patch.object(mailing, "lob_send", side_effect=lob([], refuse=("12 Palm Way",))):
        services.hand_over_queued()
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent)):
        services.mail_now(fresh(n), ppl.ann)                                                  # the address was fixed; try again
    assert sorted(s[0] for s in sent) == ["12 Palm Way"] * 2                                  # only the two that had failed
    assert not MailPiece.objects.filter(notice=n, status=MailPiece.NOT_SENT, handled_on__isnull=True).exists()
    with pytest.raises(services.WorkflowError, match="not waiting"):
        services.mail_now(fresh(n), ppl.ann)                                                  # nothing left to do


def test_HO5_a_letter_a_person_marked_handled_is_never_sent_by_the_app_afterwards(rented, deferred, ppl):
    n = queued(rented, ppl.ann)
    with mock.patch.object(mailing, "lob_send", side_effect=RuntimeError("Lob is down")):
        services.hand_over_queued()
    for p in MailPiece.objects.filter(notice=n, label="owner property", status=MailPiece.NOT_SENT):
        services.record_piece_handled(p, "Mailed from the print service's own site.", ppl.ann)
    sent = []
    lapse(n)
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent)):
        services.hand_over_queued()
    assert sorted(s[0] for s in sent) == ["PO Box 55"] * 2                                    # only the two nobody had dealt with


def test_HO5_when_a_person_handles_every_failed_letter_the_notice_stops_being_retried(rented, deferred, ppl):
    n = queued(rented, ppl.ann)
    with mock.patch.object(mailing, "lob_send", side_effect=RuntimeError("Lob is down")):
        services.hand_over_queued()
    for p in MailPiece.objects.filter(notice=n, status=MailPiece.NOT_SENT):
        services.record_piece_handled(p, "All mailed by hand.", ppl.ann)
    sent = []
    lapse(n)
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent)):
        services.hand_over_queued()
    assert sent == [] and fresh(n).mail_queued_on is None


# ======================================================================================================================================
# queued letters are not held letters
# ======================================================================================================================================
def test_HO6_a_newer_notice_of_the_same_kind_replaces_one_still_waiting(rented, deferred, ppl):
    first = queued(rented, ppl.ann)
    second = queued(rented, ppl.ann)
    first = fresh(first)
    assert first.mail_cancelled and first.mail_queued_on is None and not first.mail_waiting
    assert rented.events.filter(text__contains="mailing cancelled").exists()
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent)):
        assert services.hand_over_queued() == 1
    assert len(sent) == 4 and fresh(second).mailed_on == TODAY and fresh(first).mailed_on is None


def test_HO6_cancelling_a_meeting_cancels_the_hearing_letters_still_waiting(rented, deferred, ppl):
    Violation.objects.filter(pk=rented.pk).update(status=S.HEARING_NEEDED)
    rented.refresh_from_db()
    meeting = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=30), zoom_link="https://z.example/9")
    services.assign_to_meeting(rented, meeting, ppl.ann)
    n = Notice.objects.get(violation=rented, kind=Notice.Kind.HEARING)
    services.cancel_meeting(meeting, ppl.ann, notify=False)
    assert fresh(n).mail_cancelled
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent)):
        assert services.hand_over_queued() == 0 and services.send_due_mailings() == 0
    assert sent == []


def test_HO6_moving_a_meeting_sends_only_the_new_date(rented, deferred, ppl):
    Violation.objects.filter(pk=rented.pk).update(status=S.HEARING_NEEDED)
    rented.refresh_from_db()
    meeting = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=30), zoom_link="https://z.example/9")
    services.assign_to_meeting(rented, meeting, ppl.ann)
    services.reschedule_meeting(meeting, timezone.now() + timedelta(days=40), ppl.ann, notify=True)
    notices = list(Notice.objects.filter(violation=rented, kind=Notice.Kind.HEARING).order_by("pk"))
    assert len(notices) == 2 and notices[0].mail_cancelled and notices[1].mail_queued
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent)):
        services.hand_over_queued()
    assert len(sent) == 4


def test_HO6_a_decision_letter_still_goes_even_if_the_case_is_closed_before_it_is_handed_over(rented, deferred, ppl):
    Violation.objects.filter(pk=rented.pk).update(status=S.FINE_APPROVED, fine_amount=100, committee_votes_for=3, committee_votes_against=0,
                                                  committee_findings="Seen.", ledger_posted_on=TODAY, fine_due_on=TODAY + timedelta(days=35))
    rented.refresh_from_db()
    n = services.send_notice(rented, Notice.Kind.FINE, ppl.ann)
    Violation.objects.filter(pk=rented.pk).update(status=S.CLOSED, cured_on=TODAY)
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent)):
        assert services.send_due_mailings() == 1
    assert len(sent) >= 2 and fresh(n).mailed_on == TODAY                                   # (a HELD copy for a closed case is cancelled; a queued decision letter is not)


def test_HO6_held_copies_are_still_job_only_and_still_cancelled_when_the_case_is_corrected(rented, deferred, ppl):
    held = Notice.objects.create(violation=rented, kind=Notice.Kind.VIOLATION, mail_hold_until=TODAY - timedelta(days=1))
    future = Notice.objects.create(violation=rented, kind=Notice.Kind.SECOND, mail_hold_until=TODAY + timedelta(days=2))
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent)):
        assert services.hand_over_queued() == 0 and sent == []                                 # the background hand-over never touches held copies
        assert services.send_due_mailings() == 1                                               # the daily job sends the one whose wait is over
    assert fresh(held).mailed_on == TODAY and fresh(future).mailed_on is None and len(sent) == 2
    Violation.objects.filter(pk=rented.pk).update(status=S.CURED, cured_on=TODAY)
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent)):
        Notice.objects.filter(pk=future.pk).update(mail_hold_until=TODAY - timedelta(days=1))
        assert services.send_due_mailings() == 0
    assert fresh(future).mail_cancelled


def test_HO6_the_daily_job_hands_over_whatever_is_still_queued(rented, deferred):
    n = queued(rented)
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent)):
        call_command("daily_jobs")
    assert len(sent) == 4 and fresh(n).mailed_on == TODAY


# ======================================================================================================================================
# the background thread
# ======================================================================================================================================
def test_HO7_asking_for_a_hand_over_while_one_is_running_makes_it_run_once_more_and_never_a_second_thread(monkeypatch):
    started = []
    monkeypatch.setattr(services, "_start_thread", lambda target: started.append(target))
    monkeypatch.setattr(services, "_kick_state", {"running": False, "again": False})
    passes = []

    def hand_over():
        passes.append(1)
        if len(passes) == 1:
            services._kick()                                                                   # a notice was queued while the first pass was working
        return 0
    monkeypatch.setattr(services, "hand_over_queued", hand_over)
    services._kick()
    services._kick()
    assert len(started) == 1                                                                   # one thread, however many times it is asked
    started[0]()                                                                               # run it: it must do a second pass for the late request
    assert len(passes) == 2 and services._kick_state == {"running": False, "again": False}
    services._kick()
    assert len(started) == 2                                                                   # and it can be started again later


def test_HO7_an_error_in_the_background_hand_over_is_logged_and_never_kills_the_thread_state(monkeypatch, caplog):
    monkeypatch.setattr(services, "_kick_state", {"running": False, "again": False})
    monkeypatch.setattr(services, "hand_over_queued", mock.Mock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(services, "_start_thread", lambda target: target())
    services._kick()
    assert services._kick_state["running"] is False and "failed" in caplog.text


# ======================================================================================================================================
# the request's own writes must not undo the hand-over
# ======================================================================================================================================
def test_HO8_the_rest_of_send_notice_never_overwrites_what_the_hand_over_recorded(rented, deferred, ppl):
    from django.core.mail import EmailMessage
    sent = []
    real = EmailMessage.send

    def send_then_hand_over(self, *a, **kw):
        out = real(self, *a, **kw)
        with mock.patch.object(mailing, "lob_send", side_effect=lob(sent)):
            services.hand_over_queued()                                                        # the background thread finishes while the request is still emailing
        return out
    with mock.patch.object(EmailMessage, "send", send_then_hand_over):
        n = queued(rented, ppl.ann)
    n = fresh(n)
    assert len(sent) == 4 and n.mailed_on == TODAY and n.mail_queued_on is None and n.provider == "lob" and n.provider_ids
    assert n.emailed_at is not None and n.emailed_to


# ======================================================================================================================================
# what people see
# ======================================================================================================================================
def test_HO9_the_case_page_shows_a_waiting_notice_with_a_mail_now_button_and_it_works(rented, deferred, ppl):
    n = queued(rented, ppl.ann)
    page = web(ppl.ann).get(f"/violations/{rented.pk}/").content.decode()
    assert "Being handed to the print service" in page and 'name="notice" value="%d"' % n.pk in page and "Mail now" in page
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent)):
        web(ppl.ann).post(f"/violations/{rented.pk}/mail_now/", {"notice": n.pk}, follow=True)
    assert len(sent) == 4 and fresh(n).mailed_on == TODAY
    assert "Being handed to the print service" not in web(ppl.ann).get(f"/violations/{rented.pk}/").content.decode()


def test_HO9_the_dashboard_flags_a_letter_that_has_been_waiting_too_long_and_not_a_fresh_one(rented, deferred, ppl):
    n = queued(rented, ppl.ann)
    assert "waiting more than" not in web(ppl.ann).get("/workspace/").content.decode()          # just queued: normal, say nothing
    Notice.objects.filter(pk=n.pk).update(mail_queued_on=timezone.now() - timedelta(minutes=30))
    page = web(ppl.ann).get("/workspace/").content.decode()
    assert "1 letter has been waiting more than 15 minutes to go to the print service" in page and "daily job" in page and "Mail now" in page
    with mock.patch.object(mailing, "lob_send", side_effect=lob([])):
        services.hand_over_queued()
    assert "waiting more than" not in web(ppl.ann).get("/workspace/").content.decode()


def test_HO9_the_owner_is_not_told_anything_false_while_the_letters_wait(rented, deferred, ppl):
    n = queued(rented, ppl.ann)
    lines = [line for item in services.correspondence(rented) if item["kind"] == "letter" for line in item["lines"]]
    assert any("being sent" in line.lower() for line in lines) and not any("mailed" in line.lower() and "by US Mail" in line for line in lines)
    staff = [line for item in services.correspondence(rented, staff=True) if item["kind"] == "letter" for line in item["lines"]]
    assert any("print service" in line.lower() for line in staff)
    assert fresh(n).mail_queued


def test_HO9_the_assign_pages_say_the_letters_are_on_their_way_in_background_mode(rented, deferred, ppl):
    Violation.objects.filter(pk=rented.pk).update(status=S.HEARING_NEEDED)
    meeting = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=30), zoom_link="https://z.example/9")
    r = web(ppl.ann).post(f"/violations/{rented.pk}/assign/", {"meeting": meeting.pk}, follow=True)
    text = " | ".join(str(m) for m in r.context["messages"])
    assert "handed to the print service" in text and "Sent to the Compliance Committee" in text


@pytest.mark.django_db(transaction=True)
def test_HO7_the_real_background_thread_hands_the_letters_over_after_the_request_has_finished(rented, deferred, ppl):
    if connection.vendor != "postgresql":
        pytest.skip("real threads need PostgreSQL (SQLite's test database cannot be shared between threads); run with DATABASE_URL set")
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lob(sent)):
        n = queued(rented, ppl.ann)                                                            # (no transaction here, so the request's work is saved at once)
        assert fresh(n).mail_queued_on is not None or fresh(n).mailed_on is not None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not fresh(n).mailed_on:
            time.sleep(0.1)
        while time.monotonic() < deadline and services._kick_state["running"]:
            time.sleep(0.05)
    n = fresh(n)
    assert n.mailed_on == TODAY and n.mail_queued_on is None and len(sent) == 4 and len(set(sent)) == 4
    assert services._kick_state == {"running": False, "again": False}
