"""Review pass 4 (R4-19): the hand-over of letters to the print service under stress: many notices, several workers handing over at the same moment (the background thread, the daily job,
"Mail now"), a print service that is slow and refuses some letters at random. Whatever happens, no letter may go twice, and no letter may be lost without being listed for a person.
Real threads on PostgreSQL (SQLite cannot lock a row; this skips there). The print service is a fake: nothing leaves this machine."""
import os
import random
import threading
import time
from collections import Counter
from datetime import timedelta
from unittest import mock

import pytest
from django.db import connection, connections
from django.utils import timezone

from violations import mailing, services
from violations.models import MailPiece, Notice, Violation
from violations.tests.test_tenant_mail import rented  # noqa: F401  (an owner with a mailing and a property address; Lob on)

pytestmark = pytest.mark.django_db(transaction=True)
S = Violation.Status


@pytest.fixture(autouse=True)
def _setup(settings, monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("row-level concurrency needs PostgreSQL; run with DATABASE_URL set")
    settings.MAIL_BACKGROUND = True
    settings.MAIL_LEASE_MINUTES = 15
    monkeypatch.setattr(services, "_kick", lambda: None)                  # (this test starts its own workers)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a test tried to reach the network")))


def test_R4_19_no_letter_goes_twice_and_none_is_lost_unlisted_when_workers_and_failures_pile_up(rented):
    rng = random.Random(int(os.environ.get("HOA_STRESS_SEED", "20261123")))          # (a fixed seed; set HOA_STRESS_SEED to try others)
    cases = [rented] + [Violation.objects.create(owner=rented.owner, vtype=rented.vtype, description="d", cure_required="c", status=S.HEARING_SCHEDULED,
                                                 proposed_fine=100, hearing_date=rented.hearing_date) for _ in range(7)]
    notices = [services.send_notice(v, Notice.Kind.HEARING, None) for v in cases]          # eight hearing notices, four letters each (two addresses, certified + first class)
    assert all(n.mail_queued_on for n in Notice.objects.filter(pk__in=[n.pk for n in notices]))
    lock, accepted = threading.Lock(), Counter()

    def flaky_lob(to, pdf, description, extra=None):
        time.sleep(rng.uniform(0.01, 0.04))
        with lock:
            roll = rng.random()
        if roll < 0.15:
            raise RuntimeError("Lob said: address is undeliverable")
        key = (description, extra)                      # "Case N hearing (owner mailing, certified)": one key per letter
        with lock:
            accepted[key] += 1
            return {"id": f"ltr_{sum(accepted.values())}", "tracking_number": "9407100000000000000001" if extra else ""}

    def worker(job):
        try:
            for _ in range(3):
                job()
        except Exception:      # noqa: BLE001
            pass
        finally:
            connections.close_all()

    def mail_now_some():
        for n in rng.sample(notices, 4):
            try:
                services.mail_now(Notice.objects.get(pk=n.pk), None)
            except services.WorkflowError:
                pass

    with mock.patch.object(mailing, "lob_send", side_effect=flaky_lob):
        jobs = [services.hand_over_queued] * 4 + [services.send_due_mailings, mail_now_some]
        ts = [threading.Thread(target=worker, args=(j,)) for j in jobs]
        [t.start() for t in ts]
        [t.join(timeout=120) for t in ts]
    assert max(accepted.values(), default=0) == 1, accepted.most_common(3)               # no letter was accepted by the print service twice
    # every letter of every notice is either on record as sent, listed as NOT sent for a person, or its notice is still queued to be tried again
    for n in notices:
        n.refresh_from_db()
        sent = set(MailPiece.objects.filter(notice=n, by_hand=False).exclude(status=MailPiece.NOT_SENT).values_list("label", "service"))
        listed = set(MailPiece.objects.filter(notice=n, status=MailPiece.NOT_SENT, handled_on__isnull=True).values_list("label", "service"))
        every = {(label, svc) for label in ("owner mailing", "owner property") for svc in ("certified", "first_class")}
        assert sent | listed == every or n.mail_queued_on is not None, (n.pk, sent, listed, n.mail_queued_on)
        assert not (sent & listed)                                                         # never both "sent" and "NOT sent"
    # second wave: the print service works; the claims have lapsed. Everything still outstanding goes out, each letter exactly once overall.
    Notice.objects.update(mail_claimed_at=timezone.now() - timedelta(minutes=30))
    with mock.patch.object(mailing, "lob_send", side_effect=lambda to, pdf, d, extra=None: accepted.update([(d, extra)]) or {"id": "ltr_x", "tracking_number": ""}):
        services.hand_over_queued()
        services.send_due_mailings()
        for n in notices:
            Notice.objects.filter(pk=n.pk).update(mail_claimed_at=timezone.now() - timedelta(minutes=30))
            try:
                services.mail_now(Notice.objects.get(pk=n.pk), None)          # "Try the failed letters again"
            except services.WorkflowError:
                pass
    assert max(accepted.values()) == 1, accepted.most_common(3)                  # still nothing twice, however many tries it took
    assert len(accepted) == 8 * 4                                                # and all 32 letters went out in the end
    assert not MailPiece.objects.filter(status=MailPiece.NOT_SENT, handled_on__isnull=True).exists()
