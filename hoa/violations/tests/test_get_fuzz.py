"""Throw awkward query-string values at every page a Board member (and a committee member) can open: a typo or a hand-edited link must give a
normal page or a plain message, never an HTTP 500. (On PostgreSQL this also catches NUL characters, which SQLite silently accepts.)

Each page is first opened with EVERY known parameter set to the same awkward value (fast), and then each parameter alone with a few
of the nastiest values, so a failure is reported as "page + parameter + value"."""
import pytest
from django.test import Client

from violations.tests.test_access_matrix import COMMITTEE_OR_STAFF, STAFF_ONLY, url_for, world  # noqa: F401

pytestmark = pytest.mark.django_db

PARAMS = ["q", "status", "type", "from", "to", "range", "year", "month", "page", "sort", "order", "v", "id", "pdf", "overdue", "reinspect", "next", "ai", "tab"]
ALL_VALUES = ["abc", "-1", "0", "99999999999999999999", "%00", "a\x00b", "<script>alert(1)</script>", "1e999", "2026-13-45", "0000-00-00", "' OR '1'='1",
              "😀" * 20, "x" * 600, "../../etc/passwd", "\n", "NaN", "None"]
NASTY_VALUES = ["abc", "99999999999999999999", "a\x00b", "2026-13-45", "1e999"]
# pages that are POST-only, downloads of stored files, or send/modify something when opened are left to the other tests
SKIP_NAMES = {"violation_action", "notice_mailed", "mail_piece_action", "board_bulk", "committee_vote", "meeting_assign_selected", "meeting_assign_all",
              "committee_meeting_save", "committee_submit", "committee_case_record", "legal_refer", "site_visit_add", "results_review", "minutes_approve"}


def get_names(world):
    from violations import urls as vu
    names = [n for n in dict.fromkeys(STAFF_ONLY + COMMITTEE_OR_STAFF) if n not in SKIP_NAMES]
    out = []
    for n in names:
        p = next((p for p in vu.urlpatterns if p.name == n), None)
        if p is not None and "<" in str(p.pattern) and not ("<int:pk>" in str(p.pattern) or "<str:action>" in str(p.pattern)):
            continue                      # a path shape the helper can't fill (not in this app today)
        out.append(n)
    return out


@pytest.mark.parametrize("who", ["board", "committee"])
def test_awkward_query_strings_never_crash_a_page(world, who):
    c = Client(raise_request_exception=False)
    c.force_login(world["users"][who])
    bad = {}
    for name in get_names(world):
        base = url_for(name, world["ids"])
        for v in ALL_VALUES:                                   # every parameter at once
            if c.get(base, {p: v for p in PARAMS}).status_code >= 500:
                bad.setdefault((name, "(all parameters)"), []).append(v[:12])
        for p in PARAMS:                                       # then one parameter at a time
            for v in NASTY_VALUES:
                if c.get(base, {p: v}).status_code >= 500:
                    bad.setdefault((name, p), []).append(v[:12])
    assert not bad, "\n".join(f"{name} ?{p}= {vals}" for (name, p), vals in sorted(bad.items()))
