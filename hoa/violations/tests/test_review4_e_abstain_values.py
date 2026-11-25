"""Review pass 4 (R4-05): A committee member's 'abstain' box holding odd digits crashed the page (HTTP 500).
Every test here FAILED on the code before its fix. Nothing is sent anywhere (Django's in-memory outbox; the print service is never called)."""
from datetime import date, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from violations.models import (CommitteeMeeting, CommitteeMember, Violation)
from violations.tests.review4_support import board, browser_form, future_meeting, make_case  # noqa: F401  (board is a fixture)

pytestmark = pytest.mark.django_db
S = Violation.Status
TODAY = date.today()


@pytest.mark.parametrize("junk", ["²", "٣", "9" * 5000, "1e5", "-1", " "])
def test_R4_05_an_odd_abstain_value_is_ignored_not_a_server_error(board, junk):
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() - timedelta(days=1), zoom_link="https://z.example/1")
    CommitteeMember.objects.create(name="Mem One", year=timezone.localtime(m.starts_at).year)
    v = make_case(S.HEARING_SCHEDULED, committee_meeting=m, hearing_date=m.starts_at, proposed_fine=100)
    r = board.post(reverse("committee_case_record", args=[v.pk]),
                   {"outcome": "confirm", "votes_for": "3", "votes_against": "0", "findings": "x", "abstain": junk})
    assert r.status_code == 302
    v.refresh_from_db()
    assert v.committee_outcome == "confirm"
