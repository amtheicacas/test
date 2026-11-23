"""Review pass 4 (R4-18): a committee member saving a decision at the very moment another member presses "Submit results". The decision was saved without locking the case, from
a copy loaded earlier, so it could write the case's OLD stage back over the stage "Submit" had just set: the meeting said "submitted" while the case was back at "sent to committee",
where nobody could reach it (the committee can no longer edit, the Board's review lists only submitted cases). Real threads on PostgreSQL (SQLite cannot lock a row; these skip there)."""
import threading
import time
from datetime import timedelta
from unittest import mock

import pytest
from django.db import connection, connections
from django.utils import timezone

from violations import services
from violations.models import CommitteeMeeting, Violation
from violations.tests.review4_support import make_case

pytestmark = pytest.mark.django_db(transaction=True)      # (real threads need real commits)
S = Violation.Status


@pytest.fixture
def _only_on_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("row-level concurrency needs PostgreSQL; run with DATABASE_URL set")


@pytest.mark.usefixtures("_only_on_postgresql")
def test_R4_18_a_decision_saved_while_the_results_are_being_submitted_cannot_undo_the_submission():
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() - timedelta(days=1), zoom_link="https://z.example/1")
    v = make_case(S.HEARING_SCHEDULED, committee_meeting=m, hearing_date=m.starts_at, proposed_fine=100, committee_outcome="confirm",
                  committee_votes_for=3, committee_votes_against=0, committee_findings="first findings")
    real = services._check_votes
    errors = []

    def slow_check(*a, **k):          # the member's save is slow right after its stage check: the other member's "Submit" runs in that gap
        time.sleep(1.0)
        return real(*a, **k)

    def save_decision():
        try:
            services.committee_record(Violation.objects.get(pk=v.pk), "amend", None, "80", 3, 0, "amended findings")
        except Exception as e:      # noqa: BLE001
            errors.append(("record", repr(e)))
        finally:
            connections.close_all()

    def submit():
        try:
            time.sleep(0.4)
            services.submit_meeting_results(CommitteeMeeting.objects.get(pk=m.pk), None)
        except Exception as e:      # noqa: BLE001
            errors.append(("submit", repr(e)))
        finally:
            connections.close_all()

    with mock.patch.object(services, "_check_votes", slow_check):
        ts = [threading.Thread(target=save_decision), threading.Thread(target=submit)]
        [t.start() for t in ts]
        [t.join(timeout=60) for t in ts]
    v.refresh_from_db()
    m.refresh_from_db()
    assert not errors, errors
    assert m.results_status == "submitted" and v.status == S.LEDGER_PENDING                  # the submission stands: the case is where the Board can reach it
    assert v.committee_outcome in ("confirm", "amend")                                        # and the decision is the one that was last saved before it


# ---- sending a result back to the committee: the case and its meeting move together (SQLite is enough: no threads) ----
def test_R4_18_sending_a_result_back_to_the_committee_never_leaves_the_case_returned_and_the_meeting_still_submitted(settings):
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() - timedelta(days=1), zoom_link="https://z.example/1", results_status="submitted")
    v = make_case(S.LEDGER_PENDING, committee_meeting=m, hearing_date=m.starts_at, proposed_fine=100, fine_amount=100, committee_outcome="confirm")
    with mock.patch.object(CommitteeMeeting, "save", side_effect=RuntimeError("the database stopped")):
        with pytest.raises(RuntimeError):
            services.board_review_item(v, None, "return", note="Please look at the photos again")
    v.refresh_from_db()
    m.refresh_from_db()
    assert (v.status, m.results_status) in ((S.LEDGER_PENDING, "submitted"), (S.HEARING_SCHEDULED, "open"))       # both moved, or neither
    assert v.status == S.LEDGER_PENDING                                                                      # (here the meeting could not be saved, so neither moved)
