"""Tests for the owner's answers to the second review (REVIEW_REPORT.md, "Answers recorded 2026-11-22").

1. A Board member (or manager) never decides their OWN household's case, but may still move it along.
2. Closing a case whose fine is already posted to the owner's ledger warns, and needs a note.
3. A letter that did not go out shows in the red "Certified mail problems" box.
4. Only the daily job hands held mail to the print service (not a page load).
9. The login heading.
"""
import types
from datetime import date, timedelta

import pytest
from django.test import Client
from django.utils import timezone

from violations import services
from violations.models import CommitteeMeeting, Event, Owner, Profile, Response, Violation, ViolationType
from violations.tests.review4_support import body

S = Violation.Status
TODAY = date.today()
pytestmark = pytest.mark.django_db


def _user(model, name, role, owner=None):
    u = model.objects.create_user(name, f"{name}@example.com", "pw", first_name=name.title())
    Profile.objects.create(user=u, role=role, owner=owner)
    return u


@pytest.fixture
def ppl(django_user_model):
    """Bob is a Board member who also OWNS 3 Dual Way (one login). Ann is another Board member, Mia a manager; neither owns a home here.
    Max is a manager who ALSO owns a home (the rule follows the home, not the job title)."""
    vt = ViolationType.objects.create(name="Dual", default_cure_days=15)
    bobs_home = Owner.objects.create(name="Bob Board", property_address="3 Dual Way", email="bob@example.com")
    maxs_home = Owner.objects.create(name="Max Manager", property_address="4 Dual Way", email="max@example.com")
    olly = Owner.objects.create(name="Olly Other", property_address="2 Dual Way", email="olly@example.com")
    past = CommitteeMeeting.objects.create(starts_at=timezone.now() - timedelta(hours=3), zoom_link="https://z.example/1")
    future = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=30), zoom_link="https://z.example/2")
    p = types.SimpleNamespace(vt=vt, bobs_home=bobs_home, maxs_home=maxs_home, olly=olly, past=past, future=future)
    p.bob = _user(django_user_model, "bob", "board", bobs_home)
    p.ann = _user(django_user_model, "ann", "board")
    p.mia = _user(django_user_model, "mia", "manager")
    p.max = _user(django_user_model, "max", "manager", maxs_home)

    def case(owner, status, **kw):
        extra = dict(cure_deadline=TODAY + timedelta(days=10), proposed_fine=100)
        if status in (S.HEARING_SCHEDULED, S.LEDGER_PENDING):
            extra.update(committee_meeting=past, hearing_date=past.starts_at)
        if status == S.LEDGER_PENDING:
            extra.update(committee_outcome="confirm", fine_amount=100, committee_votes_for=3, committee_votes_against=0)
        if status == S.FINE_APPROVED:
            extra.update(fine_amount=100, ledger_posted_on=TODAY, ledger_reference="V-1", fine_due_on=TODAY + timedelta(days=30))
        extra.update(kw)
        return Violation.objects.create(owner=owner, vtype=vt, description="d", cure_required="c", status=status, **extra)
    p.case = case
    return p


# ======================================================================================================================================
# 1. Your own household's case: no decision that lets it off or delays it; moving it along is still fine
# ======================================================================================================================================
DECISIONS = [
    ("dismiss it", S.NOTICE_SENT, {}, lambda v, u: services.dismiss(v, u, "no violation")),
    ("mark it corrected", S.NOTICE_SENT, {}, lambda v, u: services.mark_cured(v, u, "fixed")),
    ("verify the re-inspection as corrected", S.NOTICE_SENT, {"reinspect_on": TODAY}, lambda v, u: services.reinspect_result(v, True, u, "ok")),
    ("verify the re-inspection as not corrected", S.NOTICE_SENT, {"reinspect_on": TODAY}, lambda v, u: services.reinspect_result(v, False, u, "no")),
    ("give more time", S.NOTICE_SENT, {}, lambda v, u: services.grant_extension(v, None, u, "why", days=5)),
    ("give more time at the Board stage", S.PENDING_BOARD, {}, lambda v, u: services.grant_extension(v, None, u, "why", days=5)),
    ("put it on hold", S.NOTICE_SENT, {}, lambda v, u: services.put_on_hold(v, "away", TODAY + timedelta(days=5), u)),
    ("record a Board vote to dismiss", S.PENDING_BOARD, {}, lambda v, u: services.board_decide(v, "dismiss", TODAY, u, "no")),
    ("record the committee's decision (case page)", S.HEARING_SCHEDULED, {}, lambda v, u: services.committee_decide(v, 3, 0, u, findings="f")),
    ("record the committee's decision (meeting page)", S.HEARING_SCHEDULED, {}, lambda v, u: services.committee_record(v, "waive", u, findings="f")),
]


@pytest.mark.parametrize("label,status,extra,call", DECISIONS, ids=[d[0] for d in DECISIONS])
def test_DEC2_nobody_makes_this_decision_on_their_own_households_case(ppl, label, status, extra, call):
    for who, home in (("bob", ppl.bobs_home), ("max", ppl.maxs_home)):          # a Board member, and a manager who owns a home
        v = ppl.case(home, status, **extra)
        before, events = Violation.objects.filter(pk=v.pk).values().get(), v.events.count()
        with pytest.raises(services.WorkflowError, match="your own household"):
            call(v, getattr(ppl, who))
        assert Violation.objects.filter(pk=v.pk).values().get() == before          # nothing changed
        assert v.events.count() == events                                          # and nothing was written to the history


@pytest.mark.parametrize("label,status,extra,call", DECISIONS, ids=[d[0] for d in DECISIONS])
def test_DEC2_the_same_decision_is_still_made_by_another_board_member_or_the_manager(ppl, label, status, extra, call):
    for who in ("ann", "mia", "max"):                                                 # (Max is blocked only on HIS OWN home)
        v = ppl.case(ppl.bobs_home, status, **extra)
        call(v, getattr(ppl, who))


def test_DEC2_declining_your_own_households_request_for_more_time_is_refused_too(ppl):
    v = ppl.case(ppl.bobs_home, S.NOTICE_SENT)
    r = Response.objects.create(violation=v, from_resident=True, message="please", extension_requested=True, extension_days=5)
    with pytest.raises(services.WorkflowError, match="your own household"):
        services.decline_extension(v, r, "no", ppl.bob)
    r.refresh_from_db()
    assert not r.handled and r.decision == ""
    services.decline_extension(v, r, "no", ppl.ann)
    r.refresh_from_db()
    assert r.handled and r.decision == "declined"


MOVES = [
    ("refer it to the Board early", S.NOTICE_SENT, {}, lambda v, u, p: services.refer_urgent(v, u, "safety")),
    ("record the Board's vote to go ahead with a fine", S.PENDING_BOARD, {}, lambda v, u, p: services.board_decide(v, "proceed", TODAY, u, fine="100", by_majority=True)),
    ("send it to a committee meeting", S.HEARING_NEEDED, {}, lambda v, u, p: services.assign_to_meeting(v, p.future, u)),
    ("mark it for re-inspection", S.NOTICE_SENT, {}, lambda v, u, p: services.mark_reinspect(v, u, "owner says fixed")),
    ("lift a hold", S.ON_HOLD, {"held_from": "notice_sent", "hold_until": TODAY + timedelta(days=5), "hold_reason": "away", "held_on": TODAY},
     lambda v, u, p: services.resume(v, u)),
    ("refer it to the attorney", S.NOTICE_SENT, {}, lambda v, u, p: services.refer_to_attorney(v, u, "why")),
    ("approve the fine the committee confirmed", S.LEDGER_PENDING, {}, lambda v, u, p: services.finalize_imposition(v, u)),
    ("record that the fine was paid", S.FINE_APPROVED, {}, lambda v, u, p: services.record_payment(v, TODAY, u)),
]


@pytest.mark.parametrize("label,status,extra,call", MOVES, ids=[m[0] for m in MOVES])
def test_DEC2_moving_your_own_households_case_to_its_next_stage_is_still_allowed(ppl, label, status, extra, call):
    v = ppl.case(ppl.bobs_home, status, **extra)
    call(v, ppl.bob, ppl)                                                              # no error


def test_DEC2_submitting_a_meetings_results_is_a_stage_move_and_stays_allowed(ppl):
    v = ppl.case(ppl.bobs_home, S.HEARING_SCHEDULED, committee_outcome="confirm", committee_votes_for=3, committee_votes_against=0)
    services.submit_meeting_results(ppl.past, ppl.bob)
    v.refresh_from_db()
    assert v.status == S.LEDGER_PENDING


def _submitted(ppl, **kw):
    ppl.past.results_status = CommitteeMeeting.Results.SUBMITTED
    ppl.past.save()
    return ppl.case(ppl.bobs_home, S.LEDGER_PENDING, **kw)


@pytest.mark.parametrize("call", [
    lambda v, u: services.board_review_item(v, u, "modify", amount="50"),                    # lower the fine
    lambda v, u: services.board_review_item(v, u, "modify", final_outcome="waive"),          # waive it instead
    lambda v, u: services.board_review_item(v, u, "return", note="look again"),              # send it back to the committee
], ids=["lower the fine", "waive it", "send it back"])
def test_DEC2_in_the_Board_review_you_cannot_lower_waive_or_send_back_your_own_households_result(ppl, call):
    v = _submitted(ppl)
    with pytest.raises(services.WorkflowError, match="your own household"):
        call(v, ppl.bob)
    v.refresh_from_db()
    assert v.status == S.LEDGER_PENDING and v.fine_amount == 100 and v.board_outcome == ""
    call(v, ppl.ann)                                                                           # another Board member can
    v.refresh_from_db()
    assert v.status != S.LEDGER_PENDING or v.committee_outcome == ""


def test_DEC2_in_the_Board_review_your_own_fine_can_be_approved_as_the_committee_decided(ppl):
    v = _submitted(ppl)
    services.board_review_item(v, ppl.bob, "approve")
    v.refresh_from_db()
    assert v.status == S.FINE_APPROVED and v.board_outcome == "confirm"


@pytest.mark.parametrize("outcome", ["waive", "reject"])
def test_DEC2_in_the_Board_review_you_cannot_approve_a_waiver_or_rejection_of_your_own_fine(ppl, outcome):
    v = _submitted(ppl, committee_outcome=outcome, fine_amount=None, committee_votes_for=3 if outcome == "reject" else None,
                   committee_votes_against=0 if outcome == "reject" else None)
    with pytest.raises(services.WorkflowError, match="your own household"):
        services.board_review_item(v, ppl.bob, "approve")
    v.refresh_from_db()
    assert v.status == S.LEDGER_PENDING and v.board_outcome == ""
    services.board_review_item(v, ppl.ann, "approve")
    v.refresh_from_db()
    assert v.status in (S.WAIVED, S.FINE_REJECTED)


def test_DEC2_jobs_and_people_without_a_login_are_not_affected(ppl):
    v = ppl.case(ppl.bobs_home, S.NOTICE_SENT, cure_deadline=TODAY - timedelta(days=1))
    assert services.advance_expired() == 1                                                   # the daily job (no user)
    v.refresh_from_db()
    assert v.status == S.PENDING_BOARD
    services.dismiss(v, None, "by a script")                                                  # no user at all: nothing to compare
    v.refresh_from_db()
    assert v.status == S.DISMISSED


def test_DEC2_own_household_is_decided_by_the_login_s_owner_record_and_nothing_else(ppl):
    v = ppl.case(ppl.olly, S.NOTICE_SENT)
    assert not services.own_household(ppl.bob, v) and not services.own_household(ppl.ann, v) and not services.own_household(None, v)
    assert services.own_household(ppl.bob, ppl.case(ppl.bobs_home, S.DRAFT))                  # a draft about your own home counts too


# ---- through the web pages ------------------------------------------------------------------------------------------------------------
def web(user):
    c = Client(raise_request_exception=False)
    c.force_login(user)
    return c


def messages_of(response):
    return " | ".join(str(m) for m in response.context["messages"]) if response.context else ""


def test_DEC2_web_a_Board_member_who_owns_the_home_cannot_dismiss_or_mark_corrected_their_own_case(ppl):
    v = ppl.case(ppl.bobs_home, S.NOTICE_SENT)
    for action in ("dismiss", "cure", "hold", "extension"):
        r = web(ppl.bob).post(f"/violations/{v.pk}/{action}/", {"note": "x", "reason": "x", "until": str(TODAY + timedelta(days=5)), "days": "5"}, follow=True)
        assert "your own household" in messages_of(r), action
    v.refresh_from_db()
    assert v.status == S.NOTICE_SENT and not v.cured_on
    r = web(ppl.ann).post(f"/violations/{v.pk}/dismiss/", {"note": "x"}, follow=True)
    v.refresh_from_db()
    assert v.status == S.DISMISSED


def test_DEC2_web_the_committee_decision_form_on_the_case_page_now_applies_the_abstention_rule(ppl):
    v = ppl.case(ppl.bobs_home, S.HEARING_SCHEDULED)
    r = web(ppl.bob).post(f"/violations/{v.pk}/committee/", {"votes_for": 3, "votes_against": 0, "findings": "Seen."}, follow=True)
    assert "your own household" in messages_of(r) or "you abstain" in messages_of(r)
    v.refresh_from_db()
    assert v.status == S.HEARING_SCHEDULED and v.committee_votes_for is None
    web(ppl.ann).post(f"/violations/{v.pk}/committee/", {"votes_for": 3, "votes_against": 0, "findings": "Seen."}, follow=True)
    v.refresh_from_db()
    assert v.status == S.LEDGER_PENDING


def test_DEC2_web_the_bulk_Board_queue_refuses_only_the_own_household_rows(ppl):
    mine, theirs = ppl.case(ppl.bobs_home, S.PENDING_BOARD), ppl.case(ppl.olly, S.PENDING_BOARD)
    r = web(ppl.bob).post("/board/bulk/", {"pick": [mine.pk, theirs.pk], f"decision_{mine.pk}": "dismiss", f"decision_{theirs.pk}": "dismiss",
                                          "date": str(TODAY)}, follow=True)
    mine.refresh_from_db(); theirs.refresh_from_db()
    assert mine.status == S.PENDING_BOARD and theirs.status == S.DISMISSED
    assert f"#{mine.pk}" in messages_of(r) and "your own household" in messages_of(r)


def test_DEC2_web_the_case_page_explains_and_hides_the_decision_buttons_for_your_own_household(ppl):
    v = ppl.case(ppl.bobs_home, S.NOTICE_SENT)
    page = web(ppl.bob).get(f"/violations/{v.pk}/").content.decode()
    assert "your own household" in page and "another Board member or the manager" in page
    for gone in ("Dismiss (no violation)", "Mark corrected", "Put on hold", "Set new deadline"):
        assert gone not in page, gone
    assert "Refer to Board now" in page and "Send to legal" in page                         # moving it along is still offered
    other = web(ppl.ann).get(f"/violations/{v.pk}/").content.decode()
    assert "your own household" not in other
    for there in ("Dismiss (no violation)", "Mark corrected", "Put on hold", "Set new deadline"):
        assert there in other, there


def test_DEC2_web_the_re_inspection_result_buttons_are_replaced_by_a_note_for_your_own_household(ppl):
    v = ppl.case(ppl.bobs_home, S.NOTICE_SENT, reinspect_on=TODAY)
    mine, theirs = web(ppl.bob).get(f"/violations/{v.pk}/").content.decode(), web(ppl.ann).get(f"/violations/{v.pk}/").content.decode()
    button = 'name="result" value="corrected"'                      # (the words also appear in a standard-reply menu, so look for the button itself)
    assert button not in mine and "Still not corrected" not in mine and button in theirs and "Still not corrected" in theirs
    assert "another Board member or the manager has to record the result of the re-inspection" in mine


def test_DEC2_web_the_committee_decision_form_is_replaced_by_a_note_for_your_own_household(ppl):
    v = ppl.case(ppl.bobs_home, S.HEARING_SCHEDULED)
    mine, theirs = web(ppl.bob).get(f"/violations/{v.pk}/").content.decode(), web(ppl.ann).get(f"/violations/{v.pk}/").content.decode()
    assert "Record decision" not in mine and "Record decision" in theirs


def test_DEC2_web_a_request_for_more_time_cannot_be_answered_by_the_household_that_made_it(ppl):
    v = ppl.case(ppl.bobs_home, S.NOTICE_SENT)
    Response.objects.create(violation=v, from_resident=True, message="please", extension_requested=True, extension_days=5)
    mine, theirs = web(ppl.bob).get(f"/violations/{v.pk}/").content.decode(), web(ppl.ann).get(f"/violations/{v.pk}/").content.decode()
    assert "ext_approve" not in mine and "ext_decline" not in mine and "ext_approve" in theirs and "ext_decline" in theirs


def test_DEC2_web_the_Board_queue_marks_the_own_household_row_and_hides_the_extension_answer_forms(ppl):
    mine = ppl.case(ppl.bobs_home, S.PENDING_BOARD)
    Response.objects.create(violation=mine, from_resident=True, message="please", extension_requested=True, extension_days=5)
    page = web(ppl.bob).get("/board/").content.decode()
    assert "your own household" in page and "ext_approve" not in page
    other = web(ppl.ann).get("/board/").content.decode()
    assert "your own household" not in other and "ext_approve" in other


def test_DEC2_web_the_review_page_offers_only_approve_as_decided_for_your_own_fine(ppl):
    v = _submitted(ppl)
    page = web(ppl.bob).get(f"/results/{ppl.past.pk}/").content.decode()
    assert "Approve as recommended" in page
    for gone in ("Return to committee", "Waive instead", "Approve with the new amount"):
        assert gone not in page, gone
    other = web(ppl.ann).get(f"/results/{ppl.past.pk}/").content.decode()
    for there in ("Approve as recommended", "Return to committee", "Waive instead", "Approve with the new amount"):
        assert there in other, there
    v.refresh_from_db()


def test_DEC2_web_the_review_page_has_no_buttons_at_all_for_your_own_waived_case(ppl):
    _submitted(ppl, committee_outcome="waive", fine_amount=None)
    page = web(ppl.bob).get(f"/results/{ppl.past.pk}/").content.decode()
    assert "Approve as recommended" not in page and "your own household" in page
    assert "Approve as recommended" in web(ppl.ann).get(f"/results/{ppl.past.pk}/").content.decode()


# ======================================================================================================================================
# 2. Closing a case whose fine is already posted to the owner's ledger: warn, and ask for a note
# ======================================================================================================================================
def _posted(ppl, status, **kw):
    """A case for someone else's home (so decision 1 does not apply) with a $100 fine already posted to the Vantaca ledger."""
    kw = {"fine_amount": 100, "ledger_posted_on": TODAY, "ledger_reference": "V-1", **kw}
    return ppl.case(ppl.olly, status, **kw)


def test_DEC1_dismissing_a_case_with_a_posted_fine_needs_a_note_and_warns(ppl):
    v = _posted(ppl, S.FINE_APPROVED)
    with pytest.raises(services.WorkflowError) as e:
        services.dismiss(v, ppl.ann, "")
    assert "$100.00 was posted to this owner's ledger" in str(e.value) and "V-1" in str(e.value) and "Write a note saying why" in str(e.value)
    v.refresh_from_db()
    assert v.status == S.FINE_APPROVED                                                       # nothing happened
    services.dismiss(v, ppl.ann, "Posted by mistake; the home was sold before the notice.")
    v.refresh_from_db()
    assert v.status == S.DISMISSED
    warning = v.events.filter(text__startswith="WARNING").get().text
    assert "$100.00" in warning and "V-1" in warning and "in Vantaca" in warning and "does not take that charge off" in warning


def test_DEC1_a_case_with_no_posted_fine_is_dismissed_exactly_as_before(ppl):
    for status in (S.NOTICE_SENT, S.PENDING_BOARD, S.HEARING_SCHEDULED):
        v = ppl.case(ppl.olly, status)
        services.dismiss(v, ppl.ann, "")                                                       # no note needed, no warning
        v.refresh_from_db()
        assert v.status == S.DISMISSED and not v.events.filter(text__startswith="WARNING").exists()


def test_DEC1_a_posted_fine_in_the_ledger_pending_state_is_not_a_thing_but_an_unposted_fine_never_warns(ppl):
    v = ppl.case(ppl.olly, S.LEDGER_PENDING)                                                   # a fine decided but NOT yet posted
    assert v.ledger_posted_on is None and services.posted_fine_warning(v) == ""
    services.dismiss(v, ppl.ann, "")
    v.refresh_from_db()
    assert v.status == S.DISMISSED


def test_DEC1_marking_a_referred_case_corrected_with_a_posted_fine_needs_a_note_and_warns(ppl):
    v = _posted(ppl, S.REFERRED_LEGAL, legal_path="after_fine", legal_referred_on=TODAY)
    with pytest.raises(services.WorkflowError, match="posted to this owner's ledger"):
        services.mark_cured(v, ppl.ann, "  ")
    v.refresh_from_db()
    assert v.status == S.REFERRED_LEGAL and v.cured_on is None
    services.mark_cured(v, ppl.ann, "Owner fixed it and paid by cheque.")
    v.refresh_from_db()
    assert v.status == S.CURED and v.events.filter(text__startswith="WARNING").exists()


def test_DEC1_marking_a_held_case_corrected_with_a_posted_fine_needs_a_note_too(ppl):
    v = _posted(ppl, S.ON_HOLD, held_from="fine_approved", hold_until=TODAY + timedelta(days=5), hold_reason="away", held_on=TODAY)
    with pytest.raises(services.WorkflowError, match="posted to this owner's ledger"):
        services.mark_cured(v, ppl.ann, "")


def test_DEC1_marking_an_imposed_case_corrected_does_not_close_it_so_it_needs_no_note_and_shows_no_warning(ppl):
    v = _posted(ppl, S.FINE_APPROVED)
    services.mark_cured(v, ppl.ann, "")                                                        # still awaiting payment: it does not close
    v.refresh_from_db()
    assert v.status == S.FINE_APPROVED and v.cured_on and not v.events.filter(text__startswith="WARNING").exists()


def test_DEC1_a_verified_re_inspection_that_closes_a_posted_case_still_warns(ppl):
    v = _posted(ppl, S.REFERRED_LEGAL, legal_path="after_fine", legal_referred_on=TODAY, reinspect_on=TODAY)
    services.reinspect_result(v, True, ppl.ann, "")                                          # its own note ("verified at re-inspection") is used
    v.refresh_from_db()
    assert v.status == S.CURED and v.events.filter(text__startswith="WARNING").exists()


def test_DEC1_web_the_case_page_warns_before_a_case_with_a_posted_fine_is_closed(ppl):
    v = _posted(ppl, S.FINE_APPROVED)
    page = web(ppl.ann).get(f"/violations/{v.pk}/").content.decode()
    assert "A fine of $100.00 was posted to this owner&#x27;s ledger in Vantaca" in page or "A fine of $100.00 was posted to this owner's ledger in Vantaca" in page
    assert 'name="note" placeholder="Reason" required' in page                                # the dismiss note is required
    quiet = web(ppl.ann).get(f"/violations/{ppl.case(ppl.olly, S.NOTICE_SENT).pk}/").content.decode()
    assert "posted to this owner" not in quiet and 'placeholder="Reason" required' not in quiet


def test_DEC1_web_dismissing_without_a_note_is_refused_with_the_warning_and_with_a_note_it_works_and_warns_again(ppl):
    v = _posted(ppl, S.FINE_APPROVED)
    r = web(ppl.ann).post(f"/violations/{v.pk}/dismiss/", {"note": ""}, follow=True)
    assert "posted to this owner's ledger" in messages_of(r)
    v.refresh_from_db()
    assert v.status == S.FINE_APPROVED
    r = web(ppl.ann).post(f"/violations/{v.pk}/dismiss/", {"note": "Posted in error."}, follow=True)
    v.refresh_from_db()
    assert v.status == S.DISMISSED and "still on the owner's ledger" in messages_of(r)



# ======================================================================================================================================
# 3. A letter the print service did not take shows in the red "Certified mail problems" box until someone handles it
# ======================================================================================================================================
import io                                                                                 # noqa: E402
import zipfile                                                                            # noqa: E402
from unittest import mock                                                                # noqa: E402

from violations import mailing                                                           # noqa: E402
from violations.models import MailPiece, Notice                                         # noqa: E402
from violations.tests.test_tenant_mail import rented  # noqa: E402,F401  (an owner with a mailing address, a property address and a tenant; Lob on)


def _lob_that_refuses(refuse_line1, sent, why="Lob said: address is undeliverable"):
    def send(to, pdf, description, extra=None):
        if to["line1"] == refuse_line1:
            raise RuntimeError(why)
        sent.append((to["line1"], extra))
        return {"id": f"ltr_{len(sent)}", "tracking_number": f"9407100000000000{len(sent):06d}"}
    return send


def _hearing_with_a_refused_property_address(rented):
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=_lob_that_refuses("12 Palm Way", sent)):
        n = services.send_notice(rented, Notice.Kind.HEARING)
    return n, sent


def _not_sent(n, **kw):
    return MailPiece.objects.filter(notice=n, status=MailPiece.NOT_SENT, **kw)


def test_LB4_a_letter_that_fails_at_the_print_service_is_listed_as_NOT_sent_while_the_others_are_recorded_as_before(rented):
    n, sent = _hearing_with_a_refused_property_address(rented)
    assert [s[0] for s in sent] == ["PO Box 55"] * 2                                         # the mailing address got both letters
    n.refresh_from_db()
    assert n.mailed_on == date.today()                                                         # (the notice still counts as mailed: part of it went)
    real = MailPiece.objects.filter(notice=n).exclude(status=MailPiece.NOT_SENT)
    assert real.count() == 2 and all(p.is_mailed and not p.not_sent for p in real)
    failed = _not_sent(n).order_by("service")
    assert [(p.label, p.service) for p in failed] == [("owner property", "certified"), ("owner property", "first_class")]
    for p in failed:
        assert p.not_sent and not p.is_mailed and p.problem and p.needs_attention and p.problem_on == date.today()
        assert p.mailed_on is None and p.provider_id == "" and not p.by_hand and p.handled_on is None
        assert "address is undeliverable" in p.problem_note
    warnings = [e.text for e in rented.events.filter(text__startswith="WARNING")]
    assert len(warnings) == 2 and all("NOT sent" in w and "owner property" in w for w in warnings)


def test_LB4_every_letter_failing_lists_every_letter_and_still_marks_nothing_mailed(rented):
    with mock.patch.object(mailing, "lob_send", side_effect=RuntimeError("Lob is down")):
        n = services.send_notice(rented, Notice.Kind.HEARING)
    n.refresh_from_db()
    assert n.mailed_on is None
    assert _not_sent(n).count() == 4                                                           # mailing + property address, certified + first class


def test_LB4_an_address_that_is_not_complete_and_a_letter_that_cannot_be_built_are_listed_too(rented):
    rented.owner.zip_code = ""                                                                 # the property address is incomplete
    rented.owner.save()
    real = services.render_notice_pdf

    def render(notice, who):
        if who == "owner":                                                                     # the mailing-address version will not build
            raise RuntimeError("will not build")
        return real(notice, who)
    with mock.patch.object(services, "render_notice_pdf", side_effect=render), mock.patch.object(mailing, "lob_send", side_effect=_lob_that_refuses("-", [])):
        n = services.send_notice(rented, Notice.Kind.HEARING)
    notes = sorted(p.problem_note for p in _not_sent(n))
    assert len(notes) == 4 and sum("incomplete mailing address" in x for x in notes) == 2 and sum("could not be built" in x for x in notes) == 2


def test_LB4_trying_again_does_not_pile_up_duplicates_and_a_later_success_closes_the_old_entries(rented):
    with mock.patch.object(mailing, "lob_send", side_effect=RuntimeError("Lob is down")):
        n = services.send_notice(rented, Notice.Kind.HEARING)
        mailing.dispatch(n, lambda who: b"%PDF-1.4")                                           # the daily job tries the wholly failed notice again
    assert _not_sent(n).count() == 4 and rented.events.filter(text__startswith="WARNING").count() == 4     # one entry (and one warning) per letter
    with mock.patch.object(mailing, "lob_send", side_effect=_lob_that_refuses("nowhere", [])):
        mailing.dispatch(n, lambda who: b"%PDF-1.4")                                           # now Lob works
    stale = _not_sent(n)
    assert stale.count() == 4 and all(p.handled_on == date.today() and "later try" in p.handled_note and not p.needs_attention for p in stale)
    assert MailPiece.objects.filter(notice=n).exclude(status=MailPiece.NOT_SENT).count() == 4


def test_LB4_owners_never_see_a_letter_that_was_not_sent(rented):
    _hearing_with_a_refused_property_address(rented)
    owner_items, staff_items = services.correspondence(rented), services.correspondence(rented, staff=True)
    pieces = lambda items: [p for item in items if item["kind"] == "letter" for p in item["pieces"]]            # noqa: E731
    assert not any(p.not_sent for p in pieces(owner_items)) and sum(p.not_sent for p in pieces(staff_items)) == 2
    lines = lambda items: [line for item in items if item["kind"] == "letter" for line in item["lines"]]       # noqa: E731
    assert not any("NOT SENT" in line for line in lines(owner_items)) and any("NOT SENT" in line for line in lines(staff_items))


def test_LB4_the_red_box_on_the_home_screen_lists_the_letter_and_the_menu_counts_it(ppl, rented):
    _hearing_with_a_refused_property_address(rented)
    page = web(ppl.ann).get("/workspace/").content.decode()
    box = page.split("Certified mail problems", 1)[1].split("</table>", 1)[0]
    assert "12 Palm Way" in box and "Letter NOT sent" in box and "address is undeliverable" in box
    assert "2 letters were NOT sent" in box and "could not be delivered" not in box
    assert "2 letters were NOT sent" in page.split('title="Items waiting on you', 1)[1].split('"', 1)[0]       # the menu tooltip
    assert ">2 to do<" in page.replace(" ", "").replace(">2todo<", ">2 to do<") or "2 to do" in page


def test_LB4_the_red_box_wording_for_an_undelivered_certified_letter_is_unchanged(ppl, rented):
    n, _ = _hearing_with_a_refused_property_address(rented)
    _not_sent(n).update(handled_on=date.today(), handled_note="handled")
    p = MailPiece.objects.filter(notice=n, service="certified").exclude(status=MailPiece.NOT_SENT).get()
    services.record_piece_not_delivered(p, "unclaimed", user=ppl.ann)
    page = web(ppl.ann).get("/workspace/").content.decode()
    box = page.split("Certified mail problems", 1)[1].split("</table>", 1)[0]
    assert "1 certified letter could not be delivered" in box and "NOT sent" not in box


def test_LB4_handling_a_letter_that_was_not_sent_clears_it_and_says_so_in_the_history(rented, ppl):
    n, _ = _hearing_with_a_refused_property_address(rented)
    piece = _not_sent(n, service="certified").get()
    with pytest.raises(services.WorkflowError, match="few words"):
        services.record_piece_handled(piece, "", ppl.ann)
    services.record_piece_handled(piece, "Sent by hand from the post office on Monday.", ppl.ann)
    piece.refresh_from_db()
    assert piece.handled_on == date.today() and piece.not_sent and not piece.needs_attention and piece.status == MailPiece.NOT_SENT
    text = rented.events.order_by("-pk").first().text
    assert "NOT sent" in text and "marked handled" in text and "not needed" not in text and "post office" in text
    with pytest.raises(services.WorkflowError, match="nothing to clear"):
        services.record_piece_handled(piece, "again", ppl.ann)
    page = web(ppl.ann).get("/workspace/").content.decode()
    assert "1 letter was NOT sent" in page                                                     # the first-class one is still listed


def test_LB4_a_letter_that_was_not_sent_cannot_be_recorded_as_mailed_or_delivered_by_mistake(rented, ppl):
    n, _ = _hearing_with_a_refused_property_address(rented)
    piece = _not_sent(n, service="certified").get()
    with pytest.raises(services.WorkflowError, match="never went out"):
        services.record_piece_mailed(piece, date.today(), "9407100000000000000001", ppl.ann)
    with pytest.raises(services.WorkflowError):
        services.record_piece_delivered(piece, date.today(), ppl.ann)
    with pytest.raises(services.WorkflowError):
        services.record_piece_not_delivered(piece, "unclaimed", user=ppl.ann)
    piece.refresh_from_db()
    assert piece.not_sent and piece.mailed_on is None and piece.delivered_on is None


def test_LB4_the_case_page_shows_the_not_sent_letter_with_a_way_to_mark_it_handled_and_no_delivery_button(rented, ppl):
    _hearing_with_a_refused_property_address(rented)
    page = web(ppl.ann).get(f"/violations/{rented.pk}/").content.decode()
    assert "2 letters not sent" in page
    row = page.split('id="mailing"', 1)[1].split("Letter NOT sent", 1)[1].split('<div class="piece"', 1)[0]
    assert "What was done about it" in row and "Mark handled" in row and "It was delivered after all" not in row


def test_LB4_a_letter_that_was_not_sent_is_not_a_letter_to_mail_by_hand(rented, ppl):
    _hearing_with_a_refused_property_address(rented)
    assert not MailPiece.objects.filter(by_hand=True).exists()
    assert "Letters to mail by hand" not in web(ppl.ann).get("/workspace/").content.decode()


def test_LB4_the_case_file_csv_lists_the_letter_with_its_problem(rented, ppl):
    _hearing_with_a_refused_property_address(rented)
    z = zipfile.ZipFile(io.BytesIO(body(web(ppl.ann).get(f"/violations/{rented.pk}/case-file.zip"))))
    text = " ".join(z.read(n).decode("utf-8", "replace") for n in z.namelist() if n.endswith(".csv"))
    assert "Letter NOT sent" in text


def test_LB4_a_not_sent_letter_is_handled_through_the_page_and_the_alert_goes_away(rented, ppl):
    n, _ = _hearing_with_a_refused_property_address(rented)
    for piece in _not_sent(n):
        r = web(ppl.ann).post(f"/mail-pieces/{piece.pk}/handled/", {"note": "Re-sent from Lob's own site."}, follow=True)
        assert r.status_code == 200
    page = web(ppl.ann).get("/workspace/").content.decode()
    assert "Certified mail problems" not in page


# ======================================================================================================================================
# 4. Held mail is handed to the print service by the daily job only: never inside a page load
# ======================================================================================================================================
from django.core.management import call_command                                          # noqa: E402


def _held_and_due(rented, days_ago=1):
    return Notice.objects.create(violation=rented, kind=Notice.Kind.VIOLATION, mail_hold_until=date.today() - timedelta(days=days_ago))


def _working_lob(sent):
    return lambda to, pdf, d, extra=None: sent.append(to["line1"]) or {"id": f"ltr_{len(sent)}", "tracking_number": f"9407100000000000{len(sent):06d}"}


def test_JOB1_opening_the_dashboard_the_board_queue_or_the_home_page_no_longer_hands_mail_to_the_print_service(rented, ppl):
    n, sent = _held_and_due(rented), []
    with mock.patch.object(mailing, "lob_send", side_effect=_working_lob(sent)):
        for url in ("/workspace/", "/board/", "/"):
            assert web(ppl.ann).get(url).status_code == 200
            assert web(ppl.mia).get(url).status_code == 200
    assert sent == []
    n.refresh_from_db()
    assert n.mailed_on is None and n.mail_tried_on is None and n.mail_held and not MailPiece.objects.filter(notice=n).exists()


def test_JOB1_the_page_load_still_moves_a_case_whose_cure_period_ended_to_the_Board_queue(rented, ppl):
    Violation.objects.filter(pk=rented.pk).update(status=S.NOTICE_SENT, cure_deadline=TODAY - timedelta(days=1))
    web(ppl.ann).get("/board/")
    rented.refresh_from_db()
    assert rented.status == S.PENDING_BOARD                                                    # (that has no outside effect, so it stays on page loads)


def test_JOB1_the_daily_job_does_hand_the_due_notice_over_and_only_once(rented):
    n, sent = _held_and_due(rented), []
    with mock.patch.object(mailing, "lob_send", side_effect=_working_lob(sent)):
        call_command("daily_jobs")
        call_command("daily_jobs")                                                              # a second run the same day sends nothing more
    n.refresh_from_db()
    assert len(sent) == 2 and n.mailed_on == date.today() and not n.mail_held and MailPiece.objects.filter(notice=n).count() == 2


def test_JOB1_a_notice_still_inside_its_wait_is_left_alone_by_the_job(rented):
    n = Notice.objects.create(violation=rented, kind=Notice.Kind.VIOLATION, mail_hold_until=date.today() + timedelta(days=2))
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=_working_lob(sent)):
        call_command("daily_jobs")
    n.refresh_from_db()
    assert sent == [] and n.mail_held


def test_JOB1_mail_now_on_the_case_page_still_sends_at_once(rented, ppl):
    n, sent = _held_and_due(rented), []
    with mock.patch.object(mailing, "lob_send", side_effect=_working_lob(sent)):
        web(ppl.ann).post(f"/violations/{rented.pk}/mail_now/", {"notice": n.pk}, follow=True)
    n.refresh_from_db()
    assert len(sent) == 2 and n.mailed_on == date.today()


def test_JOB1_the_dashboard_says_which_mail_is_waiting_for_the_daily_job(rented, ppl):
    page = web(ppl.ann).get("/workspace/").content.decode()
    assert "past the wait" not in page
    inside = Notice.objects.create(violation=rented, kind=Notice.Kind.VIOLATION, mail_hold_until=date.today() + timedelta(days=2))
    page = web(ppl.ann).get("/workspace/").content.decode()
    assert "1 mailed copy on hold" in page and "past the wait" not in page
    inside.mail_hold_until = date.today() - timedelta(days=1)
    inside.save()
    page = web(ppl.ann).get("/workspace/").content.decode()
    assert "1 mailed copy is past the wait and has not gone out yet" in page and "The daily job sends it" in page and "Mail now" in page
    with mock.patch.object(mailing, "lob_send", side_effect=_working_lob([])):
        call_command("daily_jobs")
    assert "past the wait" not in web(ppl.ann).get("/workspace/").content.decode()



def test_LB4_the_red_box_says_whether_each_unsent_letter_was_certified_or_first_class(ppl, rented):
    _hearing_with_a_refused_property_address(rented)
    box = web(ppl.ann).get("/workspace/").content.decode().split("Certified mail problems", 1)[1].split("</table>", 1)[0]
    assert "(certified, return receipt)" in box and "(first class)" in box


def test_DEC2_web_the_review_form_for_your_own_household_has_no_change_the_fine_box(ppl):
    _submitted(ppl)
    mine, theirs = web(ppl.bob).get(f"/results/{ppl.past.pk}/").content.decode(), web(ppl.ann).get(f"/results/{ppl.past.pk}/").content.decode()
    assert "Change the fine to" not in mine and "Note (optional)" in mine and "required to return it" not in mine
    assert "Change the fine to" in theirs and "Note (required to return it)" in theirs
