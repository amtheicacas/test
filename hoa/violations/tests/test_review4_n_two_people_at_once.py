"""Review pass 4 (R4-14): two people (or two clicks) at the same moment, with REAL threads on PostgreSQL (SQLite cannot lock a row, so these skip there; run
the suite with DATABASE_URL set). The buttons fixed in this pass must hold when the two presses overlap, not only when they follow each other."""
import threading
from datetime import date, timedelta

import pytest
from django.core import mail
from django.db import connection, connections
from django.utils import timezone

from violations import services
from violations.models import Notice, Response, Violation
from violations.tests.review4_support import future_meeting, make_case

S = Violation.Status
TODAY = date.today()
pytestmark = pytest.mark.django_db(transaction=True)


def at_the_same_time(*jobs):
    """Run each job in its own thread, released together; returns [('ok', result) | ('refused', message)] in the order of the jobs."""
    start, out = threading.Barrier(len(jobs)), [None] * len(jobs)

    def run(i, job):
        try:
            start.wait(timeout=10)
            out[i] = ("ok", job())
        except services.WorkflowError as e:
            out[i] = ("refused", str(e))
        except Exception as e:      # noqa: BLE001
            out[i] = ("crashed", f"{type(e).__name__}: {e}")
        finally:
            connections.close_all()

    ts = [threading.Thread(target=run, args=(i, j)) for i, j in enumerate(jobs)]
    [t.start() for t in ts]
    [t.join(timeout=60) for t in ts]
    return out


@pytest.fixture(autouse=True)
def _only_on_postgresql(settings):
    if connection.vendor != "postgresql":
        pytest.skip("row-level concurrency needs PostgreSQL; run with DATABASE_URL set")
    settings.MAIL_PROVIDER = "manual"


def test_R4_14_two_people_sending_the_same_case_to_the_same_meeting_at_once_send_one_hearing_notice():
    v = make_case(S.HEARING_NEEDED, proposed_fine=100, board_meeting_date=TODAY)
    m = future_meeting(30)
    results = at_the_same_time(lambda: services.assign_to_meeting(Violation.objects.get(pk=v.pk), m, None),
                               lambda: services.assign_to_meeting(Violation.objects.get(pk=v.pk), m, None))
    assert sorted(r[0] for r in results) == ["ok", "refused"], results
    assert Notice.objects.filter(violation=v, kind="hearing").count() == 1 and len(mail.outbox) == 1


def test_R4_14_two_people_moving_a_meeting_to_the_same_new_time_at_once_notify_once():
    v = make_case(S.HEARING_NEEDED, proposed_fine=100, board_meeting_date=TODAY)
    m = future_meeting(30)
    services.assign_to_meeting(v, m, None)
    before = Notice.objects.count()
    new = m.starts_at + timedelta(days=4)
    results = at_the_same_time(lambda: services.reschedule_meeting(type(m).objects.get(pk=m.pk), new, None, notify=True),
                               lambda: services.reschedule_meeting(type(m).objects.get(pk=m.pk), new, None, notify=True))
    assert sorted(r[0] for r in results) == ["ok", "refused"], results
    assert Notice.objects.count() == before + 1


def test_R4_14_two_board_members_answering_one_request_for_more_time_at_once_grant_it_only_once():
    v = make_case(S.NOTICE_SENT)
    req = Response.objects.create(violation=v, from_resident=True, message="more time please", extension_requested=True, extension_days=5)
    start = v.cure_deadline
    results = at_the_same_time(
        lambda: services.grant_extension(Violation.objects.get(pk=v.pk), None, None, "", days=5, response=Response.objects.get(pk=req.pk)),
        lambda: services.grant_extension(Violation.objects.get(pk=v.pk), None, None, "", days=5, response=Response.objects.get(pk=req.pk)),
        lambda: services.decline_extension(Violation.objects.get(pk=v.pk), Response.objects.get(pk=req.pk), "no", None))
    assert [r[0] for r in results].count("ok") == 1, results
    v.refresh_from_db()
    assert v.cure_deadline in (start + timedelta(days=5), start)                    # granted once, or declined: never granted twice
    assert Notice.objects.filter(violation=v, kind="extension").count() <= 1


def test_R4_14_the_case_page_extension_form_pressed_twice_at_once_adds_the_days_once():
    v = make_case(S.NOTICE_SENT)
    start = v.cure_deadline
    shown = start.isoformat()                                                       # what the page showed when it was opened
    results = at_the_same_time(lambda: services.grant_extension(Violation.objects.get(pk=v.pk), None, None, "", days=10, deadline_was=shown),
                               lambda: services.grant_extension(Violation.objects.get(pk=v.pk), None, None, "", days=10, deadline_was=shown))
    assert sorted(r[0] for r in results) == ["ok", "refused"], results
    v.refresh_from_db()
    assert v.cure_deadline == start + timedelta(days=10)
