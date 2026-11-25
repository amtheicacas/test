"""Review pass 4 (R4-01): Pressing Send to committee twice (a double click) sent the Notice of Hearing twice.
Every test here FAILED on the code before its fix. Nothing is sent anywhere (Django's in-memory outbox; the print service is never called)."""
from datetime import date

import pytest
from django.core import mail
from django.urls import reverse

from violations import services
from violations.models import (MailPiece, Notice, Violation)
from violations.tests.review4_support import board, browser_form, future_meeting, make_case  # noqa: F401  (board is a fixture)

pytestmark = pytest.mark.django_db
S = Violation.Status
TODAY = date.today()


def test_R4_01_pressing_Send_to_committee_twice_sends_one_hearing_notice(board, settings):
    settings.MAIL_PROVIDER = "manual"
    v = make_case(S.HEARING_NEEDED, proposed_fine=100, board_meeting_date=TODAY)
    m = future_meeting()
    url = reverse("violation_action", args=[v.pk, "assign"])
    board.post(url, {"meeting": m.pk})
    sent_once = (Notice.objects.filter(violation=v, kind="hearing").count(), len(mail.outbox), MailPiece.objects.count())
    r = board.post(url, {"meeting": m.pk}, follow=True)
    assert sent_once[0] == 1
    assert (Notice.objects.filter(violation=v, kind="hearing").count(), len(mail.outbox), MailPiece.objects.count()) == sent_once      # the second press changed nothing
    assert "already" in r.content.decode().lower()                                                                                      # ...and said why


def test_R4_01_sending_a_case_to_a_DIFFERENT_meeting_still_works(board, settings):
    settings.MAIL_PROVIDER = "manual"
    v = make_case(S.HEARING_NEEDED, proposed_fine=100, board_meeting_date=TODAY)
    m1, m2 = future_meeting(30), future_meeting(45)
    services.assign_to_meeting(v, m1, None)
    services.assign_to_meeting(v, m2, None)
    v.refresh_from_db()
    assert v.committee_meeting_id == m2.pk and Notice.objects.filter(violation=v, kind="hearing").count() == 2
