"""Follow-up after review pass 4 (owner's answer `idempotency`): every letter handed to Lob carries an `Idempotency-Key`, so that a letter Lob already
accepted when the site was stopped (a deploy, a restart: the review measured 5 letters received for 4) is not printed and mailed a second time when the
hand-over is resumed. What Lob documents (docs.lob.com, read 2026-11-25): the key goes in the `Idempotency-Key` header, may be up to 256 characters,
is remembered for 24 hours per mode (test/live) and per resource type, and a repeat returns the ORIGINAL letter instead of creating another.
Not verified against the real Lob (never run live): the first run with a Lob TEST key must confirm it (REVIEW_REPORT.md, follow-up 2026-11-25).
Nothing here reaches Lob: the network is blocked and every call is a fake."""
import re
from unittest import mock

import pytest

from violations import mailing, services
from violations.models import MailPiece, Notice
from violations.tests.test_certified_mail import ADDR, _FakeResponse
from violations.tests.test_decisions_pass3 import ppl  # noqa: F401
from violations.tests.test_handover_pass3 import Boom, _no_real_network, deferred, fresh, lapse, queued  # noqa: F401  (fixtures)
from violations.tests.test_tenant_mail import rented  # noqa: F401

pytestmark = pytest.mark.django_db
KEY = re.compile(r"^hoa-[0-9a-f]{40}$")


@pytest.fixture
def lob_on(settings):
    settings.MAIL_PROVIDER, settings.STAGING, settings.PRODUCTION = "lob", False, False
    settings.LOB_API_KEY = "test_abc123"
    settings.HOA_RETURN_LINE1, settings.HOA_RETURN_CITY, settings.HOA_RETURN_STATE, settings.HOA_RETURN_ZIP = "100 Main St #5", "Sarasota", "FL", "34236"


def _capture(box):
    def fake(req, timeout=None):
        box.append(req)
        return _FakeResponse({"id": "ltr_1"})
    return fake


def test_F5_lob_send_puts_the_key_in_the_idempotency_header(lob_on):
    seen = []
    with mock.patch("urllib.request.urlopen", _capture(seen)):
        with mailing.idempotency_key("hoa-abc"):
            mailing.lob_send(ADDR, b"%PDF-1", "Case 1")
    assert {k.lower(): v for k, v in seen[0].header_items()}["idempotency-key"] == "hoa-abc"
    assert "idempotency_key" not in seen[0].full_url          # one way only: Lob refuses a request that carries the key twice


def test_F5_without_a_key_nothing_is_added(lob_on):
    seen = []
    with mock.patch("urllib.request.urlopen", _capture(seen)):
        mailing.lob_send(ADDR, b"%PDF-1", "Case 1")
    assert "idempotency-key" not in {k.lower() for k, _ in seen[0].header_items()}


def test_F5_the_key_does_not_leak_from_one_letter_to_the_next(lob_on):
    seen = []
    with mock.patch("urllib.request.urlopen", _capture(seen)):
        with mailing.idempotency_key("hoa-first"):
            mailing.lob_send(ADDR, b"%PDF-1", "Case 1")
        mailing.lob_send(ADDR, b"%PDF-1", "Case 2")
    assert "idempotency-key" in {k.lower() for k, _ in seen[0].header_items()}
    assert "idempotency-key" not in {k.lower() for k, _ in seen[1].header_items()}


def test_F5_the_key_names_the_letter_and_nothing_secret(rented):
    n = Notice.objects.create(violation=rented, kind="hearing")
    k = mailing.letter_key(n, "owner mailing", "certified", ADDR)
    assert KEY.match(k) and rented.token not in k
    assert k == mailing.letter_key(n, "owner mailing", "certified", dict(ADDR))                          # the same letter: the same key
    assert k != mailing.letter_key(n, "owner mailing", "first_class", ADDR)                              # the other service
    assert k != mailing.letter_key(n, "owner property", "certified", ADDR)                               # the other address of the household
    assert k != mailing.letter_key(n, "owner mailing", "certified", dict(ADDR, line1="9 Other Way"))     # the address was corrected: a new letter
    assert k != mailing.letter_key(Notice.objects.create(violation=rented, kind="hearing"), "owner mailing", "certified", ADDR)
    rented.token = "another-database-same-notice-number"
    assert k != mailing.letter_key(n, "owner mailing", "certified", ADDR)                                # a copy of the site on another PC never collides


def test_F5_a_hand_over_stopped_in_the_middle_resends_the_letter_in_the_air_with_the_same_key(rented, deferred, ppl):
    n = queued(rented, ppl.ann)
    first, second = [], []

    def dies_on_the_third(to, pdf, description, extra=None):
        first.append(mailing.current_idempotency_key())
        if len(first) == 3:
            raise Boom()                       # "the site was stopped" while Lob may or may not have taken this letter
        return {"id": f"ltr_{len(first)}", "tracking_number": ""}

    def fine(to, pdf, description, extra=None):
        second.append(mailing.current_idempotency_key())
        return {"id": f"ltr_b{len(second)}", "tracking_number": ""}

    with mock.patch.object(mailing, "lob_send", side_effect=dies_on_the_third), pytest.raises(Boom):
        services.hand_over_queued()
    lapse(n)
    with mock.patch.object(mailing, "lob_send", side_effect=fine):
        services.hand_over_queued()
    assert len(second) == 2 and second[0] == first[2]                                  # the letter that was in the air carries the SAME key
    assert len(set(first + second)) == 4 and all(KEY.match(k) for k in first + second)    # four letters, four different keys


def test_F5_a_letter_the_print_service_refused_is_tried_again_with_the_same_key(rented, deferred, ppl):
    """Lob documents that a repeated key returns the existing letter; a refused request created none. (To be confirmed on the first TEST-key run.)"""
    n = queued(rented, ppl.ann)
    keys = []

    def refuse_first(to, pdf, description, extra=None):
        keys.append(mailing.current_idempotency_key())
        if len(keys) == 1:
            raise RuntimeError("Lob said: address is undeliverable")
        return {"id": f"ltr_{len(keys)}", "tracking_number": ""}

    with mock.patch.object(mailing, "lob_send", side_effect=refuse_first):
        services.hand_over_queued()
    assert MailPiece.objects.filter(notice=n, status=MailPiece.NOT_SENT).count() == 1
    again = []
    with mock.patch.object(mailing, "lob_send", side_effect=lambda to, pdf, d, extra=None: again.append(mailing.current_idempotency_key()) or {"id": "ltr_x", "tracking_number": ""}):
        services.mail_now(fresh(n), ppl.ann)
    assert again == [keys[0]]
