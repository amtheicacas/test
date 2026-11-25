"""Cross-site-scripting canary (second stabilisation pass): every free-text field a person (staff, owner, tenant, Vantaca import) can fill in is set to a
string that would break out of HTML text, an attribute or a script block, and then every page reachable by links is fetched as each kind of login.
No page may contain the string unescaped. Also: no page may carry a javascript: link."""
import re

import pytest
from django.apps import apps

from violations import services
from violations.models import CannedReply, CommitteeMeeting, CommitteeMember, Event, Owner, Response, Violation, ViolationType
from violations.tests.test_crawl_roles import crawl, people  # noqa: F401
from violations.tests.test_access_matrix import world  # noqa: F401

pytestmark = pytest.mark.django_db

TAG = "<xss-canary-7f3a>"
TAG_PATTERNS = [TAG, "</script><xss-canary-7f3a>", "<img src=x onerror=alert(1)>"]           # must never appear anywhere, even inside a JSON data block
QUOTE_PATTERNS = ['" autofocus onfocus=alert(1) x="', "' autofocus onfocus=alert(1) x='"]      # a breakout of an HTML attribute (harmless inside a JSON data block)
RAW_PATTERNS = TAG_PATTERNS + QUOTE_PATTERNS
JSON_BLOCK = re.compile(r'<script id="[^"]*" type="application/json">.*?</script>', re.S)


def raw_hits(text):
    """Which dangerous strings appear unescaped: tags anywhere; quote breakouts only outside JSON data blocks (Django's json_script escapes < > & there,
    and the page's own script puts that data on the page through an escaping function)."""
    return [p for p in TAG_PATTERNS if p in text] + [p for p in QUOTE_PATTERNS if p in JSON_BLOCK.sub("", text)]
CANARY = TAG + '" autofocus onfocus=alert(1) x="' + "' autofocus onfocus=alert(1) x='" + "</script><xss-canary-7f3a>" + "<img src=x onerror=alert(1)>"


def _set_every_text_field(model, instance_filter=None, skip=()):
    fields = [f for f in model._meta.get_fields()
              if getattr(f, "concrete", False) and not f.primary_key and f.get_internal_type() in ("CharField", "TextField") and f.name not in skip and not f.choices]
    qs = model.objects.all() if instance_filter is None else model.objects.filter(**instance_filter)
    for obj in qs:
        values = {}
        for f in fields:
            limit = f.max_length or 10_000
            values[f.name] = (CANARY[:max(limit - 8, 0)] + f"#{obj.pk}")[:limit] if (f.unique or f.name == "property_address") else CANARY[:limit]      # unique fields need a different value each
        model.objects.filter(pk=obj.pk).update(**values)


@pytest.fixture
def poisoned(people, world):
    """The shared world, with canary text in every free-text field of every record a page can show."""
    ids = world["ids"]
    _set_every_text_field(Owner, skip=("email", "state", "mail_state", "external_id"))
    Owner.objects.update(state="FL")
    _set_every_text_field(Violation, skip=("token", "status", "issuance_timing", "legal_path", "committee_outcome", "board_outcome", "held_from", "reinspect"))
    Violation.objects.update(token="tokenvalue-0123456789abcdef-" + "x")
    _set_every_text_field(ViolationType, skip=("source",))
    _set_every_text_field(CommitteeMeeting, skip=("zoom_link",))
    _set_every_text_field(CommitteeMember)
    _set_every_text_field(CannedReply)
    Response.objects.all().delete()
    v = Violation.objects.get(pk=ids["violation"])
    services.add_response(v, CANARY, from_resident=True)
    services.add_response(v, CANARY, from_resident=False)
    services.log(v, "Staff note: " + CANARY)
    CannedReply.objects.create(title="t", body=CANARY)
    return people


@pytest.mark.parametrize("who", ["anon", "homeA", "committee", "board", "manager", "dualBoard"])
def test_no_page_shows_a_text_someone_typed_without_escaping_it(poisoned, who):
    pages = crawl(poisoned[who], limit=70)
    assert len(pages) >= 2
    raw = {u: raw_hits(t) for u, (s, t) in pages.items() if raw_hits(t)}
    assert not raw, raw
    js_links = [u for u, (s, t) in pages.items() if re.search(r'href="\s*javascript:', t, re.I)]
    assert not js_links, js_links


def test_the_private_owner_link_page_escapes_everything_too(poisoned, world):
    from django.test import Client
    v = Violation.objects.get(pk=world["ids"]["violation"])
    Violation.objects.filter(pk=v.pk).update(status=Violation.Status.NOTICE_SENT)
    t = Violation.objects.get(pk=v.pk).token
    r = Client().get(f"/r/{t}/")
    text = r.content.decode()
    assert r.status_code == 200 and "&lt;xss-canary-7f3a&gt;" in text
    assert not raw_hits(text)


def test_the_canary_really_reaches_the_pages_so_the_scan_is_not_empty(poisoned):
    """If the crawl never showed the canary even escaped, the scan above would prove nothing."""
    pages = crawl(poisoned["board"], limit=70)
    assert sum("&lt;xss-canary-7f3a&gt;" in t for s, t in pages.values()) >= 4
    owner_pages = crawl(poisoned["homeA"], limit=40)
    assert sum("&lt;xss-canary-7f3a&gt;" in t for s, t in owner_pages.values()) >= 1
