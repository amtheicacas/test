"""Follow-up after review pass 4 (owner's answer `limit-deadline`): a typed EXACT new deadline more than one year away is refused (a typo such as
2062 used to be accepted and would have left the case waiting for decades). The "days to add" box and every other rule are unchanged.
Every refusal test FAILED on the code before the change. Nothing is sent anywhere."""
from datetime import date, timedelta

import pytest
from django.core import mail
from django.urls import reverse

from violations import services
from violations.models import Violation
from violations.tests.review4_support import board, browser_form, make_case  # noqa: F401  (board is a fixture)

S = Violation.Status
TODAY = date.today()
pytestmark = pytest.mark.django_db


def test_F5_an_exact_deadline_more_than_a_year_away_is_refused_and_changes_nothing():
    v = make_case()
    was = v.cure_deadline
    for typed in (date(2062, 5, 1), TODAY + timedelta(days=366), TODAY + timedelta(days=4000)):
        with pytest.raises(services.WorkflowError, match="more than a year"):
            services.grant_extension(v, new_deadline=typed, user=None)
    v.refresh_from_db()
    assert v.cure_deadline == was and v.extended_days == 0 and not mail.outbox


def test_F5_exactly_a_year_is_still_allowed():
    v = make_case()
    services.grant_extension(v, new_deadline=TODAY + timedelta(days=365), user=None, notify=False)
    v.refresh_from_db()
    assert v.cure_deadline == TODAY + timedelta(days=365)


def test_F5_a_second_notice_reset_has_the_same_limit():
    v = make_case()
    with pytest.raises(services.WorkflowError, match="more than a year"):
        services.grant_extension(v, new_deadline=TODAY + timedelta(days=800), user=None, second_notice=True, notify=False)


def test_F5_days_to_add_is_not_affected():
    v = make_case()
    v.cure_deadline = TODAY + timedelta(days=200)
    v.save()
    services.grant_extension(v, days=300, user=None, notify=False)        # 500 days out: it was always allowed (the box itself stops at 365)
    v.refresh_from_db()
    assert v.cure_deadline == TODAY + timedelta(days=500)


def test_F5_the_case_page_says_so_in_plain_words(board):
    v = make_case()
    url = reverse("violation_detail", args=[v.pk])
    action, data = browser_form(board, url, f"/violations/{v.pk}/extension/", new_deadline="2062-05-01", days="")
    r = board.post(action, data, follow=True)
    text = r.content.decode()
    assert "more than a year" in text
    v.refresh_from_db()
    assert v.cure_deadline == TODAY + timedelta(days=5) and not mail.outbox
