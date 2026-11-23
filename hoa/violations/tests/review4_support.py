"""Shared helpers for the pass-4 review tests (test_review4_*.py): a Board login, a case in any status, a future meeting, and `browser_form`, which
collects exactly what a browser would post from a form on a page (so a "double click" test posts the page's own hidden fields, not hand-made ones)."""
from datetime import date, timedelta
from html.parser import HTMLParser

import pytest
from django.test import Client
from django.utils import timezone

from violations.models import CommitteeMeeting, Owner, Profile, Violation, ViolationType

S = Violation.Status
TODAY = date.today()


@pytest.fixture
def board(django_user_model):
    u = django_user_model.objects.create_user("bd4", password="pw-for-tests-123", first_name="Bea", last_name="Board")
    Profile.objects.create(user=u, role="board")
    c = Client(raise_request_exception=False)
    c.force_login(u)
    c.user = u
    return c


def make_case(status=S.NOTICE_SENT, n=1, owner_kw=None, **kw):
    o = Owner.objects.create(name=f"Pat {n}", property_address=f"{n} Review Way", email=f"p{n}@example.com", **(owner_kw or {}))
    t = ViolationType.objects.get_or_create(name="Shed", defaults={"default_cure_days": 15})[0]
    v = Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c", cure_days=15, status=status,
                                 cure_deadline=TODAY + timedelta(days=5), **kw)
    return v


def future_meeting(days=30, **kw):
    return CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=days), zoom_link=kw.pop("zoom_link", "https://z.example/1"), **kw)


class _Forms(HTMLParser):
    """What a browser would send for the <form> whose action ends with `suffix`: every named input / textarea / select in it, as it stands."""

    def __init__(self, suffix):
        super().__init__()
        self.suffix, self.inside, self.data, self._select, self._textarea = suffix, False, {}, None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.inside = (a.get("action") or "").endswith(self.suffix)
        elif self.inside and tag == "input" and a.get("name"):
            if a.get("type") in ("checkbox", "radio") and "checked" not in a:
                return
            if a.get("type") in ("submit", "button", "file"):
                return
            self.data[a["name"]] = a.get("value", "")
        elif self.inside and tag == "select" and a.get("name"):
            self._select = a["name"]
            self.data.setdefault(a["name"], "")
        elif self.inside and tag == "option" and self._select and ("selected" in a or not self.data.get(self._select)):
            self.data[self._select] = a.get("value", "")
        elif self.inside and tag == "textarea" and a.get("name"):
            self._textarea = a["name"]
            self.data[a["name"]] = ""

    def handle_data(self, data):
        if self._textarea:
            self.data[self._textarea] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False
        elif tag == "select":
            self._select = None
        elif tag == "textarea":
            self._textarea = None


def browser_form(client, page_url, action_suffix, **overrides):
    """Open the page like a browser, collect the form that posts to `action_suffix` and return (post url, the fields it would send)."""
    html = client.get(page_url).content.decode()
    p = _Forms(action_suffix)
    p.feed(html)
    assert p.data or overrides, f"no form posting to {action_suffix} on {page_url}"
    p.data.pop("csrfmiddlewaretoken", None)
    p.data.update(overrides)
    return action_suffix, p.data
