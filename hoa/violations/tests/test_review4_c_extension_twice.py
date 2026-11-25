"""Review pass 4 (R4-03): A double click on 'Set new deadline & notify' / the Board's 'give more time' added the days twice and sent two letters.
Every test here FAILED on the code before its fix. Nothing is sent anywhere (Django's in-memory outbox; the print service is never called)."""
from datetime import date, timedelta

import pytest
from django.urls import reverse

from violations import services
from violations.models import (Notice, Response, Violation)
from violations.tests.review4_support import board, browser_form, future_meeting, make_case  # noqa: F401  (board is a fixture)

pytestmark = pytest.mark.django_db
S = Violation.Status
TODAY = date.today()


def test_R4_03_the_case_page_extension_form_pressed_twice_adds_the_days_once(board, settings):
    settings.MAIL_PROVIDER = "manual"
    v = make_case(S.NOTICE_SENT)
    page = reverse("violation_detail", args=[v.pk])
    suffix, data = browser_form(board, page, f"/violations/{v.pk}/extension/", days="10", reason="")
    start = v.cure_deadline
    board.post(suffix, data)
    board.post(suffix, data)                                                                                                    # the second click of a double click
    v.refresh_from_db()
    assert v.cure_deadline == start + timedelta(days=10) and v.extended_days == 10
    assert Notice.objects.filter(violation=v, kind="extension").count() == 1


def test_R4_03_a_second_extension_opened_afresh_is_still_allowed(board, settings):
    settings.MAIL_PROVIDER = "manual"
    v = make_case(S.NOTICE_SENT)
    page = reverse("violation_detail", args=[v.pk])
    start = v.cure_deadline
    for _ in range(2):                                                                                                          # two separate visits, each opening the page again
        suffix, data = browser_form(board, page, f"/violations/{v.pk}/extension/", days="10", reason="")
        board.post(suffix, data)
    v.refresh_from_db()
    assert v.cure_deadline == start + timedelta(days=20) and Notice.objects.filter(violation=v, kind="extension").count() == 2


def test_R4_03_the_Board_vote_to_give_more_time_pressed_twice_adds_the_days_once(board, settings):
    settings.MAIL_PROVIDER = "manual"
    v = make_case(S.PENDING_BOARD)
    start = v.cure_deadline
    data = {"decision": "extend", "meeting_date": TODAY.isoformat(), "extension_days": "10", "note": ""}
    url = reverse("violation_action", args=[v.pk, "board"])
    board.post(url, data)
    board.post(url, data)
    v.refresh_from_db()
    assert v.cure_deadline == start + timedelta(days=10) and Notice.objects.filter(violation=v, kind="extension").count() == 1


def test_R4_03_two_Board_members_answering_the_same_request_for_more_time_grant_it_once(settings):
    settings.MAIL_PROVIDER = "manual"
    v = make_case(S.NOTICE_SENT)
    req = Response.objects.create(violation=v, from_resident=True, message="more time please", extension_requested=True, extension_days=5)
    start = v.cure_deadline
    services.grant_extension(v, None, None, "", days=5, response=req)
    with pytest.raises(services.WorkflowError, match="already"):
        services.grant_extension(v, None, None, "", days=5, response=Response.objects.get(pk=req.pk))
    with pytest.raises(services.WorkflowError, match="already"):
        services.decline_extension(v, Response.objects.get(pk=req.pk), "no", None)
    v.refresh_from_db()
    assert v.cure_deadline == start + timedelta(days=5)
