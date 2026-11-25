"""Review pass 4 (R4-04): The timing of a first notice came straight from the posted form, unchecked.
Every test here FAILED on the code before its fix. Nothing is sent anywhere (Django's in-memory outbox; the print service is never called)."""
from datetime import date

import pytest

from violations import services
from violations.models import (Notice, Violation)
from violations.tests.review4_support import board, browser_form, future_meeting, make_case  # noqa: F401  (board is a fixture)

pytestmark = pytest.mark.django_db
S = Violation.Status
TODAY = date.today()


@pytest.mark.parametrize("bad", ["whenever", "x" * 40, "STANDARD", "urgent "])
def test_R4_04_an_unknown_notice_timing_is_refused_not_saved(board, bad, settings):
    settings.MAIL_PROVIDER = "manual"
    from violations.models import Photo
    from django.core.files.base import ContentFile
    v = make_case(S.DRAFT)
    Photo.objects.create(violation=v, image=ContentFile(b"x", name="p.jpg"))
    with pytest.raises(services.WorkflowError, match="timing"):
        services.issue_first_notice(v, board.user, timing=bad)
    v.refresh_from_db()
    assert v.status == S.DRAFT and v.issuance_timing == "standard" and Notice.objects.count() == 0
