"""Review pass 4 (R4-02): Moving a meeting to the time it already has resent every owner's Notice of Hearing.
Every test here FAILED on the code before its fix. Nothing is sent anywhere (Django's in-memory outbox; the print service is never called)."""
from datetime import date, timedelta

import pytest
from django.core import mail
from django.utils import timezone

from violations import services
from violations.models import (Notice, Violation)
from violations.tests.review4_support import board, browser_form, future_meeting, make_case  # noqa: F401  (board is a fixture)

pytestmark = pytest.mark.django_db
S = Violation.Status
TODAY = date.today()


def test_R4_02_moving_a_meeting_to_the_time_it_already_has_sends_nothing(board, settings):
    settings.MAIL_PROVIDER = "manual"
    v = make_case(S.HEARING_NEEDED, proposed_fine=100, board_meeting_date=TODAY)
    m = future_meeting(30)
    services.assign_to_meeting(v, m, None)
    before = (Notice.objects.count(), len(mail.outbox))
    with pytest.raises(services.WorkflowError, match="already"):
        services.reschedule_meeting(m, timezone.localtime(m.starts_at), board.user, notify=True)
    assert (Notice.objects.count(), len(mail.outbox)) == before


def test_R4_02_moving_a_meeting_to_a_new_time_still_notifies(board, settings):
    settings.MAIL_PROVIDER = "manual"
    v = make_case(S.HEARING_NEEDED, proposed_fine=100, board_meeting_date=TODAY)
    m = future_meeting(30)
    services.assign_to_meeting(v, m, None)
    services.reschedule_meeting(m, m.starts_at + timedelta(days=3), board.user, notify=True)
    assert Notice.objects.filter(violation=v, kind="hearing").count() == 2
