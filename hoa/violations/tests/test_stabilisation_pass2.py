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
