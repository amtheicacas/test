"""Tests added by the second stabilisation / security pass (Nov 2026). Each test names the problem it pins down (ids as in REVIEW_REPORT.md)."""
import pytest
from django.test import Client
from django.urls import reverse

from violations.tests.test_access_matrix import world  # noqa: F401  (the shared fixture: owners, a case, a meeting, every kind of login)

pytestmark = pytest.mark.django_db


def board_client(world, who="board"):
    c = Client(raise_request_exception=False)
    c.force_login(world["users"][who])
    return c


# ---- GET-1: a hand-edited violation-list link must give a normal page, never HTTP 500 ----------------------------------------------------------
@pytest.mark.parametrize("query", ["type=abc", "type=99999999999999999999", "type=1e999", "from=2026-13-45", "to=abc", "from=0000-00-00&to=2026-02-30", "from=a&to=b&type=c"])
def test_violation_list_ignores_a_junk_type_or_date_instead_of_crashing(world, query):
    r = board_client(world).get(reverse("violation_list") + "?" + query)
    assert r.status_code == 200
    assert "Alex" in r.content.decode()           # the junk filter is simply not applied: the case is still listed


def test_violation_list_real_filters_still_work(world):
    c = board_client(world)
    from violations.models import Violation
    v = Violation.objects.get()
    assert "Alex" in c.get(reverse("violation_list"), {"type": v.vtype_id}).content.decode()
    assert "Alex" not in c.get(reverse("violation_list"), {"type": v.vtype_id + 1}).content.decode()
    assert "Alex" not in c.get(reverse("violation_list"), {"from": "2999-01-01"}).content.decode()
    assert "Alex" in c.get(reverse("violation_list"), {"to": "2999-01-01"}).content.decode()


# ---- NUL-1: PostgreSQL cannot store a NUL (0x00) character; typing one used to be an HTTP 500 there (SQLite accepts it, which hid the problem) ------
def _case_with_a_private_link(world):
    from violations.models import Owner, Violation, ViolationType
    o = Owner.objects.get(name="Alex")
    t = ViolationType.objects.get()
    return Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c", status=Violation.Status.NOTICE_SENT)


def test_a_nul_character_in_a_posted_message_is_removed_before_anything_is_saved(world):
    v = _case_with_a_private_link(world)
    r = Client(raise_request_exception=False).post(f"/r/{v.token}/", {"message": "ab\x00cd"})
    assert r.status_code == 302
    assert [x.message for x in v.responses.all()] == ["abcd"]


def test_a_nul_character_in_a_search_box_is_removed(world):
    r = board_client(world).get("/violations/", {"q": "a\x00b"})
    assert r.status_code == 200
    assert 'value="ab"' in r.content.decode() and "\x00" not in r.content.decode()


@pytest.mark.parametrize("url", ["/r/abc%00def/", "/violations/%00/", "/m%00e/", "/accounts/link/ab%00cd/", "/%00"])
def test_a_nul_character_in_the_address_is_simply_page_not_found(world, url):
    for who in (None, "board"):
        c = board_client(world, who) if who else Client(raise_request_exception=False)
        assert c.get(url).status_code == 404


def test_the_nul_filter_cleans_get_and_post_and_leaves_clean_requests_untouched():
    from django.http import HttpResponse
    from django.test import RequestFactory

    from violations.middleware import StripNulCharacters
    mw = StripNulCharacters(lambda request: HttpResponse("ok"))
    rf = RequestFactory()
    req = rf.post("/x/?a=1&b%00=2%003&a=4", {"k\x00": ["v\x001", "v2"], "plain": "p"})
    assert mw(req).status_code == 200
    mw.process_view(req, None, (), {})
    assert dict(req.GET.lists()) == {"a": ["1", "4"], "b": ["23"]}
    assert dict(req.POST.lists()) == {"k": ["v1", "v2"], "plain": ["p"]}
    with pytest.raises(AttributeError):
        req.POST["new"] = "x"                                  # still read-only, like a normal request's form data
    clean = rf.get("/x/?a=1")
    before = clean.GET
    mw(clean)
    assert clean.GET is before                                  # nothing to remove: the very same object


# ======================================================================================================================================
# Mail dispatch safety (LB-1, LB-2, KEY-1, LB-5)
# ======================================================================================================================================
import threading
import time
from datetime import date, timedelta
from unittest import mock

from django.db import connection, connections

from violations import mailing, services
from violations.models import MailPiece, Notice, Violation
from violations.tests.test_certified_mail import ADDR, _FakeResponse
from violations.tests.test_tenant_mail import rented  # noqa: F401  (an owner with a mailing address, a property address and a tenant, MAIL_PROVIDER=lob)


def _fake_lob(sent):
    return lambda to, pdf, d, extra=None: sent.append((to["line1"], extra)) or {"id": f"ltr_{len(sent)}", "tracking_number": f"9407100000000000{len(sent):06d}"}


def test_LB1_one_letter_that_cannot_be_built_does_not_stop_the_others_or_lose_the_record_of_those_already_sent(rented):
    real, sent = services.render_notice_pdf, []

    def render(notice, who):
        if who == "owner_property":
            raise RuntimeError("this letter will not build")
        return real(notice, who)
    with mock.patch.object(services, "render_notice_pdf", side_effect=render), mock.patch.object(mailing, "lob_send", side_effect=_fake_lob(sent)):
        n = services.send_notice(rented, Notice.Kind.HEARING)              # (used to raise: the letters to the mailing address had already gone to Lob)
    n.refresh_from_db()
    assert [s[0] for s in sent] == ["PO Box 55"] * 2                      # certified + first class to the mailing address went
    assert MailPiece.objects.filter(notice=n).count() == 2                # ...and both are on record
    assert n.mailed_on == date.today() and "owner property" in n.provider_note and "could not be built" in n.provider_note
    assert rented.events.filter(text__contains="could not be built").exists()      # the case history says which address did not go


def test_LB1_when_every_letter_fails_to_build_nothing_is_marked_mailed_and_nothing_is_lost(rented):
    sent = []
    with mock.patch.object(services, "render_notice_pdf", side_effect=RuntimeError("no")), mock.patch.object(mailing, "lob_send", side_effect=_fake_lob(sent)):
        n = services.send_notice(rented, Notice.Kind.VIOLATION)
    n.refresh_from_db()
    assert sent == [] and n.mailed_on is None and n.provider_note.count("could not be built") == 2


def test_LB2_a_stale_second_mail_now_click_does_not_send_the_letters_again(rented):
    n = Notice.objects.create(violation=rented, kind=Notice.Kind.VIOLATION, mail_hold_until=date.today() + timedelta(days=3))
    first, second = Notice.objects.get(pk=n.pk), Notice.objects.get(pk=n.pk)      # two people's pages, both loaded before either clicked
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=_fake_lob(sent)):
        services.mail_now(first)
        count = len(sent)
        with pytest.raises(services.WorkflowError):
            services.mail_now(second)
    assert count == 2 and len(sent) == 2 and MailPiece.objects.filter(notice=n).count() == 2


@pytest.mark.django_db(transaction=True)
def test_LB2_two_page_loads_at_the_same_moment_release_a_held_notice_only_once(rented):
    """advance_expired runs on every dashboard / Board-queue page load; two loads at once used to BOTH release the same held notice."""
    if connection.vendor != "postgresql":
        pytest.skip("row locks need PostgreSQL; run with DATABASE_URL set")
    n = Notice.objects.create(violation=rented, kind=Notice.Kind.VIOLATION, mail_hold_until=date.today() - timedelta(days=1))
    sent = []

    def slow(to, pdf, d, extra=None):
        sent.append(to["line1"])
        time.sleep(0.4)
        return {"id": f"ltr_{len(sent)}"}

    def load_the_page():
        try:
            services.advance_expired(date.today())
        finally:
            connections.close_all()
    with mock.patch.object(mailing, "lob_send", side_effect=slow):
        ts = [threading.Thread(target=load_the_page) for _ in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    assert len(sent) == 2, sent                                           # the mailing address and the property, once each: never doubled
    assert MailPiece.objects.filter(notice=n).count() == 2


S_NOTICE_SENT = Violation.Status.NOTICE_SENT


def test_ADV1_advance_expired_does_not_overwrite_an_extension_made_after_it_looked(rented):
    """The list of expired cases is read once; staff can extend one of them while an earlier case is being moved. The extension must win."""
    yesterday = date.today() - timedelta(days=1)
    Violation.objects.filter(pk=rented.pk).update(status=S_NOTICE_SENT, cure_deadline=yesterday)
    twin = Violation.objects.create(owner=rented.owner, vtype=rented.vtype, description="d", cure_required="c", status=S_NOTICE_SENT, cure_deadline=yesterday)
    extended, real_log = [], services.log

    def log_then_extend_the_other_case(v, text, user=None):
        real_log(v, text, user)
        if text.startswith("Cure period ended") and not extended:
            other = twin if v.pk == rented.pk else rented
            Violation.objects.filter(pk=other.pk).update(cure_deadline=date.today() + timedelta(days=10))      # staff click 'Give more time'
            extended.append(other.pk)
    with mock.patch.object(services, "log", side_effect=log_then_extend_the_other_case):
        moved = services.advance_expired(date.today())
    kept = Violation.objects.get(pk=extended[0])
    assert moved == 1 and kept.status == S_NOTICE_SENT and kept.cure_deadline == date.today() + timedelta(days=10)
    assert Violation.objects.exclude(pk=extended[0]).get(pk__in=[rented.pk, twin.pk]).status == Violation.Status.PENDING_BOARD


def test_KEY1_a_lob_key_with_stray_spaces_is_sent_trimmed(settings):
    settings.MAIL_PROVIDER, settings.STAGING, settings.PRODUCTION, settings.LOB_API_KEY = "lob", False, False, "  test_abc123 \n"
    settings.HOA_RETURN_LINE1, settings.HOA_RETURN_CITY, settings.HOA_RETURN_STATE, settings.HOA_RETURN_ZIP = "100 Main St", "Sarasota", "FL", "34236"
    seen = {}

    def fake(req, timeout=None):
        seen["auth"] = req.get_header("Authorization")
        return _FakeResponse({"id": "ltr_1"})
    import base64

    class Ok(_FakeResponse):
        status = 200
    with mock.patch("urllib.request.urlopen", lambda req, timeout=None: (fake(req, timeout), Ok({"id": "ltr_1"}))[1]):
        mailing.lob_send(ADDR, b"%PDF-1", "Case 1")
        mailing.lob_ping()
        assert base64.b64decode(seen["auth"].split()[1]) == b"test_abc123:"
        mailing.lob_get("ltr_1")
        assert base64.b64decode(seen["auth"].split()[1]) == b"test_abc123:"


def test_LB5_a_failed_tracking_check_is_reported_not_hidden_but_never_breaks_the_job(rented, caplog):
    n = Notice.objects.create(violation=rented, kind=Notice.Kind.HEARING)
    piece = MailPiece.objects.create(notice=n, label="owner mailing", service="certified", provider_id="ltr_1", mailed_on=date.today())
    with mock.patch.object(mailing, "lob_get", side_effect=RuntimeError("Lob is down")):
        errors = []
        assert services.refresh_mail_tracking(errors=errors) == 0
        assert errors == [f"letter {piece.pk}: RuntimeError"] and f"mail piece {piece.pk} (RuntimeError)" in caplog.text
        from django.core.management import call_command
        out = __import__("io").StringIO()
        call_command("daily_jobs", stdout=out)                                      # the job still completes (a provider hiccup never breaks it)
    assert "0 certified letter(s) had tracking updates. 1 tracking check(s) could NOT be completed" in out.getvalue()


def test_ADV1_a_hold_lifted_by_someone_else_a_moment_ago_does_not_break_the_page_load(rented):
    """lift_expired_holds reads the list once; staff may lift one of the holds first. That must not raise (the dashboard runs this on every load)."""
    yesterday = date.today() - timedelta(days=1)
    Violation.objects.filter(pk=rented.pk).update(status=Violation.Status.ON_HOLD, held_from=S_NOTICE_SENT, hold_until=yesterday, hold_reason="r")
    twin = Violation.objects.create(owner=rented.owner, vtype=rented.vtype, description="d", cure_required="c", status=Violation.Status.ON_HOLD,
                                    held_from=S_NOTICE_SENT, hold_until=yesterday, hold_reason="r")
    lifted, real_log = [], services.log

    def log_then_lift_the_other_hold(v, text, user=None):
        real_log(v, text, user)
        if text.startswith("Hold ended") and not lifted:
            other = twin if v.pk == rented.pk else rented
            Violation.objects.filter(pk=other.pk).update(status=S_NOTICE_SENT, held_from="", hold_until=None)       # staff click 'Lift hold'
            lifted.append(other.pk)
    with mock.patch.object(services, "log", side_effect=log_then_lift_the_other_hold):
        assert services.lift_expired_holds(date.today()) == 1
    assert Violation.objects.get(pk=lifted[0]).status == S_NOTICE_SENT
