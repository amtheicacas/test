"""Review pass 4 (R4-06): After a refused second approval the Board's review screen showed an error AND a green 'done' line.
Every test here FAILED on the code before its fix. Nothing is sent anywhere (Django's in-memory outbox; the print service is never called)."""
from datetime import date, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from violations.models import (CommitteeMeeting, Violation)
from violations.tests.review4_support import board, browser_form, future_meeting, make_case  # noqa: F401  (board is a fixture)

pytestmark = pytest.mark.django_db
S = Violation.Status
TODAY = date.today()


def test_R4_06_a_refused_second_approval_shows_only_the_error(board, settings):
    settings.MAIL_PROVIDER = "manual"
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() - timedelta(days=1), zoom_link="https://z.example/1", results_status="submitted")
    v = make_case(S.LEDGER_PENDING, committee_meeting=m, hearing_date=m.starts_at, proposed_fine=100, fine_amount=100, committee_outcome="confirm")
    url = reverse("results_review", args=[v.pk])
    board.post(url, {"action": "approve", "method": "manual", "reference": "R1"}, follow=True)          # (follow it, so its own messages are shown and used up)
    second = board.post(url, {"action": "approve", "method": "manual", "reference": "R1"}, follow=True)
    texts = [(str(x), x.level_tag) for x in second.context["messages"]]
    assert any(level == "error" for _, level in texts), texts
    assert not any(level == "success" for _, level in texts), texts
