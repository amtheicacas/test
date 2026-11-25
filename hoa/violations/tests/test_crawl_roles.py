"""Crawl the site as each kind of login: start at the home page and follow every same-site link (GET only), up to a page limit.
No page may crash (HTTP 5xx or a traceback), an owner must never reach another owner's name or address, and an owner page must never
contain words that belong to staff-only screens. Built for the stabilisation pass: it finds pages nobody thought to test."""
import re
from datetime import timedelta
from urllib.parse import urlsplit

import pytest
from django.test import Client
from django.utils import timezone

from violations.models import CommitteeMember, Owner
from violations.tests.test_access_matrix import mk, world  # noqa: F401  (the shared fixture: owners A and B, a case on a meeting, a notice, a letter, a photo...)

pytestmark = pytest.mark.django_db
HREF = re.compile(r'href="(/[^"#]*)"')
SKIP = ("/static/", "/media/", "/accounts/logout", f"/{'admin'}/", "/healthz")
STAFF_ONLY_WORDS = ("Site-visit observations", "Mailing log and print copies", "Owner history", "Audit log", "Staff logins", "To be mailed",
                    "Certified mail problems", "Exact copies of letters sent")


def crawl(user, limit=70):
    c = Client(raise_request_exception=False)
    if user is not None:
        c.force_login(user)
    seen, queue, pages = set(), ["/"], {}
    while queue and len(pages) < limit:
        url = queue.pop(0)
        if url in seen or any(url.startswith(s) for s in SKIP):
            continue
        seen.add(url)
        r = c.get(url)
        text = r.content.decode("utf-8", "replace") if "html" in r.get("Content-Type", "") or r.status_code >= 300 else ""
        if r.status_code in (301, 302) and r.get("Location", "").startswith("/"):
            queue.append(r["Location"])
        pages[url] = (r.status_code, text)
        for href in HREF.findall(text):
            parts = urlsplit(href)
            clean = parts.path + (f"?{parts.query}" if parts.query else "")
            if clean not in seen:
                queue.append(clean)
    return pages


@pytest.fixture
def people(world):
    oa, ob = Owner.objects.get(pk=world["ids"]["owner"]), Owner.objects.exclude(pk=world["ids"]["owner"]).get()
    dual_board = mk("dualb", "board", owner=oa)                      # an owner who is also on the Board (one login)
    dual_comm = mk("dualc", "committee", owner=ob)                   # an owner who is also on the committee
    CommitteeMember.objects.create(name="Dual C", year=timezone.localdate().year, user=dual_comm, owner=ob)
    return {**world["users"], "dualBoard": dual_board, "dualCommittee": dual_comm}


@pytest.mark.parametrize("who", ["anon", "homeA", "homeB", "committee", "board", "manager", "dualBoard", "dualCommittee"])
def test_no_page_reachable_by_links_crashes_for_any_kind_of_login(people, who):
    pages = crawl(people[who])
    bad = {u: s for u, (s, t) in pages.items() if s >= 500 or "Traceback (most recent call last)" in t or "Server Error (500)" in t}
    assert not bad, bad
    assert len(pages) >= 1


@pytest.mark.parametrize("who,mine,theirs", [("homeA", "1 A Way", ("2 B Way", "Bea")), ("homeB", "2 B Way", ("1 A Way", "Alex"))])
def test_an_owner_never_reaches_another_owners_name_or_address_by_following_links(people, who, mine, theirs):
    pages = crawl(people[who])
    assert any(mine in t for _, t in pages.values())                 # the crawl really reached the owner's own pages
    leaks = {u: w for u, (s, t) in pages.items() for w in theirs if w in t}
    assert not leaks, leaks


@pytest.mark.parametrize("who", ["anon", "homeA", "homeB"])
def test_owner_and_visitor_pages_never_contain_staff_only_words(people, who):
    pages = crawl(people[who])
    found = {u: w for u, (s, t) in pages.items() for w in STAFF_ONLY_WORDS if w in t}
    assert not found, found


@pytest.mark.parametrize("who,at_least", [("anon", 3), ("homeA", 6), ("homeB", 3), ("committee", 6), ("board", 30), ("manager", 30),
                                           ("dualBoard", 30), ("dualCommittee", 8)])
def test_the_crawl_really_reaches_pages_for_every_kind_of_login(people, who, at_least):
    """Guards the crawler itself: if a template change stops the links being followed, this fails instead of the crawl silently testing nothing."""
    assert len(crawl(people[who])) >= at_least
