"""Review pass 4 (R4-15): the decision letter is created AFTER the fine is imposed and posted to the ledger (so a letter problem can never undo the posting). If creating it
failed for any reason, the page crashed with a server error and nothing was recorded: the fine stood, the owner was never sent the letter, and nobody could tell.
Every test here FAILED on the code before its fix. Nothing is sent anywhere."""
from datetime import timedelta
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone

from violations import services
from violations.models import CommitteeMeeting, Notice, Violation
from violations.tests.review4_support import board, make_case  # noqa: F401  (board is a fixture)

pytestmark = pytest.mark.django_db
S = Violation.Status


def submitted_case(outcome="confirm"):
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() - timedelta(days=1), zoom_link="https://z.example/1", results_status="submitted")
    return make_case(S.LEDGER_PENDING, committee_meeting=m, hearing_date=m.starts_at, proposed_fine=100, fine_amount=100, committee_outcome=outcome), m


def texts(response):
    return [(str(x), x.level_tag) for x in response.context["messages"]]


def test_R4_15_approving_a_fine_when_the_decision_letter_cannot_be_created_keeps_the_fine_and_says_so(board, settings):
    settings.MAIL_PROVIDER = "manual"
    v, m = submitted_case("confirm")
    with mock.patch.object(services, "send_notice", side_effect=RuntimeError("the database hiccuped")):
        r = board.post(reverse("results_review", args=[v.pk]), {"action": "approve", "method": "manual", "reference": "R1"}, follow=True)
    assert r.status_code == 200
    v.refresh_from_db()
    assert v.status == S.FINE_APPROVED and v.ledger_posted_on is not None                           # the posting stands
    assert any("WARNING" in e.text and "decision" in e.text.lower() and "NOT" in e.text for e in v.events.all())
    shown = texts(r)
    assert any(level == "error" and "NOT been sent" in text for text, level in shown), shown        # the person is told plainly
    assert not any(level == "success" for _, level in shown), shown
    m.refresh_from_db()
    assert m.results_status == "processed"                                                       # and the meeting's review is still finished off


def test_R4_15_the_case_page_impose_button_does_not_crash_either(board, settings):
    settings.MAIL_PROVIDER = "manual"
    v, m = submitted_case("confirm")
    with mock.patch.object(services, "send_notice", side_effect=RuntimeError("boom")):
        r = board.post(reverse("violation_action", args=[v.pk, "impose"]), {"method": "manual", "reference": "R2"}, follow=True)
    assert r.status_code == 200
    v.refresh_from_db()
    assert v.status == S.FINE_APPROVED and any("WARNING" in e.text for e in v.events.all())
    assert any(level == "error" and "NOT been sent" in text for text, level in texts(r))


def test_R4_15_a_rejection_when_the_decision_letter_cannot_be_created_is_recorded_and_the_meeting_still_finishes(board, settings):
    settings.MAIL_PROVIDER = "manual"
    v, m = submitted_case("reject")
    with mock.patch.object(services, "send_notice", side_effect=RuntimeError("boom")):
        r = board.post(reverse("results_review", args=[v.pk]), {"action": "approve", "note": ""}, follow=True)
    assert r.status_code == 200
    v.refresh_from_db()
    assert v.status == S.FINE_REJECTED and any("WARNING" in e.text and "NOT" in e.text for e in v.events.all())
    assert any(level == "error" and "NOT been sent" in text for text, level in texts(r))
    m.refresh_from_db()
    assert m.results_status == "processed"


def test_R4_15_when_nothing_goes_wrong_nothing_changes(board, settings):
    settings.MAIL_PROVIDER = "manual"
    v, m = submitted_case("confirm")
    r = board.post(reverse("results_review", args=[v.pk]), {"action": "approve", "method": "manual", "reference": "R3"}, follow=True)
    v.refresh_from_db()
    assert v.status == S.FINE_APPROVED and Notice.objects.filter(violation=v, kind="fine").count() == 1
    assert not any(level == "error" for _, level in texts(r)) and not any("WARNING" in e.text for e in v.events.all())
