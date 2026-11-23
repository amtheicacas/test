"""Review pass 4 (R4-07): A tracking update wrote back a stale copy of the whole notice (blanking what another part of the app had just saved).
Every test here FAILED on the code before its fix. Nothing is sent anywhere (Django's in-memory outbox; the print service is never called)."""
from datetime import date

import pytest
from django.utils import timezone

from violations import mailing, services
from violations.models import (MailPiece, Notice, Violation)
from violations.tests.review4_support import board, browser_form, future_meeting, make_case  # noqa: F401  (board is a fixture)

pytestmark = pytest.mark.django_db
S = Violation.Status
TODAY = date.today()


def test_R4_07_applying_tracking_does_not_overwrite_the_email_record_saved_meanwhile():
    v = make_case(S.FINE_APPROVED, fine_amount=100)
    stale = Notice.objects.create(violation=v, kind="fine")                      # the hand-over loaded the notice here, before the email part finished
    Notice.objects.filter(pk=stale.pk).update(emailed_at=timezone.now(), emailed_to="p1@example.com", email_error="")      # ...which then saved its result
    piece = MailPiece.objects.create(notice=stale, label="owner property", service="certified", provider_id="ltr_1")
    mailing.apply_tracking(piece, {"tracking_number": "9407100000000000000001", "tracking_events": []})
    fresh = Notice.objects.get(pk=stale.pk)
    assert fresh.emailed_to == "p1@example.com" and fresh.emailed_at is not None        # not blanked
    assert fresh.tracking_number == "9407100000000000000001"                              # and the tracking number still reached the notice


def test_R4_07_recording_a_mailing_by_hand_does_not_overwrite_what_was_saved_meanwhile():
    v = make_case(S.FINE_APPROVED, fine_amount=100)
    stale = Notice.objects.create(violation=v, kind="fine")
    Notice.objects.filter(pk=stale.pk).update(emailed_at=timezone.now(), emailed_to="p1@example.com", mail_claimed_at=timezone.now())
    services.mark_mailed(stale, TODAY, "9407100000000000000009", None, "certified")
    fresh = Notice.objects.get(pk=stale.pk)
    assert fresh.emailed_to == "p1@example.com" and fresh.mail_claimed_at is not None                  # not blanked by the stale copy
    assert fresh.mailed_on == TODAY and fresh.tracking_number == "9407100000000000000009" and fresh.method == "certified"


def test_R4_07_working_out_a_notice_from_its_letters_does_not_overwrite_what_was_saved_meanwhile():
    v = make_case(S.FINE_APPROVED, fine_amount=100)
    stale = Notice.objects.create(violation=v, kind="hearing")
    MailPiece.objects.create(notice=stale, label="owner property", service="certified", by_hand=True, mailed_on=TODAY, tracking_number="9407100000000000000009")
    Notice.objects.filter(pk=stale.pk).update(emailed_to="x@example.com", mail_claimed_at=timezone.now())
    services._sync_notice_from_pieces(stale)
    fresh = Notice.objects.get(pk=stale.pk)
    assert fresh.emailed_to == "x@example.com" and fresh.mail_claimed_at is not None
    assert fresh.mailed_on == TODAY and fresh.tracking_number == "9407100000000000000009"
