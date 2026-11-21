"""Owner decisions after the stabilisation review (REVIEW_REPORT.md F-08 and F-05): one test per behaviour."""
import io
from datetime import date
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from violations import ledger, services
from violations.models import CommitteeMeeting, Event, Notice, Owner, Photo, Profile, Violation, ViolationType
from violations.tests.test_ledger import pending

pytestmark = pytest.mark.django_db
S = Violation.Status


# ---------- F-08: two approvals at once must post the fine to the ledger only once ----------
def test_a_second_approval_from_a_stale_page_is_refused_and_posts_nothing(settings):
    v = pending(fine=150)
    first, second = Violation.objects.get(pk=v.pk), Violation.objects.get(pk=v.pk)      # two people loaded the same page
    with mock.patch.object(ledger, "post_fine", return_value="TX-1") as post:
        services.finalize_imposition(first, method="api", today=date(2026, 12, 1))
        with pytest.raises(services.WorkflowError):
            services.finalize_imposition(second, method="api", today=date(2026, 12, 1))
    assert post.call_count == 1
    v.refresh_from_db()
    assert v.status == S.FINE_APPROVED and v.ledger_reference == "TX-1"
    assert v.notices.filter(kind=Notice.Kind.FINE).count() == 1


def test_the_posting_and_the_new_status_are_saved_together_and_a_retry_never_posts_again():
    v = pending(fine=150)
    with mock.patch.object(ledger, "post_fine", return_value="TX-2") as post:
        services.finalize_imposition(v, method="api", today=date(2026, 12, 1))
    v.refresh_from_db()
    assert (v.ledger_posted_on, v.status) == (date(2026, 12, 1), S.FINE_APPROVED)
    # a case that was posted earlier but is still waiting (older data) is approved without posting again
    w = pending(fine=150, ext="V-78")
    w.ledger_posted_on, w.ledger_reference = date(2026, 11, 30), "OLD-1"
    w.save()
    with mock.patch.object(ledger, "post_fine") as post2:
        services.finalize_imposition(Violation.objects.get(pk=w.pk), method="api", today=date(2026, 12, 1))
    assert post2.call_count == 0
    assert Violation.objects.get(pk=w.pk).status == S.FINE_APPROVED


def test_a_failed_posting_changes_nothing_and_can_be_retried():
    v = pending(fine=150)
    with mock.patch.object(ledger, "post_fine", side_effect=ledger.LedgerError("Vantaca is down")):
        with pytest.raises(services.WorkflowError):
            services.finalize_imposition(v, method="api")
    v.refresh_from_db()
    assert v.status == S.LEDGER_PENDING and v.ledger_posted_on is None and not v.notices.filter(kind=Notice.Kind.FINE).exists()


def test_the_board_review_page_cannot_be_used_twice_on_the_same_case():
    """board_review_item: a stale second 'approve' must not write the old status back or post again."""
    v = pending(fine=150)
    m = CommitteeMeeting.objects.create(starts_at=v.hearing_date, zoom_link="https://z.example/1", results_status=CommitteeMeeting.Results.SUBMITTED)
    v.committee_meeting, v.committee_outcome = m, "confirm"
    v.save()
    a, b = Violation.objects.get(pk=v.pk), Violation.objects.get(pk=v.pk)
    with mock.patch.object(ledger, "post_fine", return_value="TX-3") as post:
        services.board_review_item(a, None, "approve", method="api")
        with pytest.raises(services.WorkflowError):
            services.board_review_item(b, None, "approve", method="api")
    v.refresh_from_db()
    assert post.call_count == 1 and v.status == S.FINE_APPROVED and v.ledger_posted_on is not None


# ---------- F-05: an unreadable photo is no longer silent ----------
def _case_with_photo(readable=True):
    o = Owner.objects.create(name="Pat", property_address="5 Photo Way", email="p@example.com")
    t = ViolationType.objects.get_or_create(name="Shed", defaults={"default_cure_days": 15})[0]
    v = Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c", status=S.DRAFT)
    b = io.BytesIO()
    Image.new("RGB", (30, 20)).save(b, "PNG")
    Photo.objects.create(violation=v, image=SimpleUploadedFile("a.png", b.getvalue() if readable else b"not a picture", content_type="image/png"))
    return v


def test_a_readable_photo_gives_no_warning(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    v = _case_with_photo(True)
    services.issue_first_notice(v)
    assert not Event.objects.filter(violation=v, text__startswith="WARNING: the violation photo").exists()


def test_an_unreadable_photo_puts_a_visible_warning_on_the_case_and_the_letter_still_goes(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    v = _case_with_photo(False)
    mail.outbox.clear()
    services.issue_first_notice(v)
    w = Event.objects.filter(violation=v, text__startswith="WARNING: the violation photo")
    assert w.count() == 1 and "WITHOUT its photo" in w.first().text
    assert len(mail.outbox) == 1          # behaviour unchanged: the letter is still sent


def test_a_missing_photo_file_also_warns(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    v = _case_with_photo(True)
    ph = v.photos.first()
    (tmp_path / ph.image.name).unlink()
    services.send_notice(v, Notice.Kind.VIOLATION)
    assert Event.objects.filter(violation=v, text__startswith="WARNING: the violation photo").exists()


def test_a_decision_letter_does_not_use_the_photo_so_it_does_not_warn(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    v = _case_with_photo(False)
    services.send_notice(v, Notice.Kind.FINE)
    assert not Event.objects.filter(violation=v, text__startswith="WARNING: the violation photo").exists()


# ---------- F-08, the real thing: two approvals at the SAME moment (needs PostgreSQL; skipped on SQLite) ----------
import threading
import time

from django.db import connection, connections


@pytest.mark.django_db(transaction=True)
def test_two_simultaneous_approvals_post_the_fine_to_the_ledger_only_once():
    if connection.vendor != "postgresql":
        pytest.skip("row locks need PostgreSQL; run with DATABASE_URL set")
    v = pending(fine=150)
    calls, results = [], []

    def slow_post(viol, today):
        calls.append(1)
        time.sleep(0.5)                     # long enough for the other approval to arrive while this one is posting
        return "TX-RACE"

    def approve():
        try:
            w = Violation.objects.get(pk=v.pk)           # each person's page loaded the case before either clicked
            results.append(("ok", services.finalize_imposition(w, method="api", today=date(2026, 12, 1)) is not None))
        except services.WorkflowError as e:
            results.append(("refused", str(e)))
        finally:
            connections.close_all()

    with mock.patch.object(ledger, "post_fine", side_effect=slow_post):
        ts = [threading.Thread(target=approve) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    assert sorted(r[0] for r in results) == ["ok", "refused"], results
    assert len(calls) == 1
    v.refresh_from_db()
    assert v.status == S.FINE_APPROVED and v.ledger_reference == "TX-RACE"
    assert v.notices.filter(kind=Notice.Kind.FINE).count() == 1
