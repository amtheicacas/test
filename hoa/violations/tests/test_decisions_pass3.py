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
