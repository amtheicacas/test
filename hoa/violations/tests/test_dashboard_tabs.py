"""The staff dashboard's "Needs your attention" cards are grouped under status tabs (the tab bar itself is built by a small script)."""
import re
from datetime import timedelta

import pytest
from django.utils import timezone

from violations.models import Owner, Profile, Response, Violation, ViolationType

pytestmark = pytest.mark.django_db
S = Violation.Status


@pytest.fixture
def page(client, django_user_model):
    u = django_user_model.objects.create_user("boss", password="x")
    Profile.objects.update_or_create(user=u, defaults={"role": "board"})
    client.force_login(u)
    o = Owner.objects.create(name="Pat Sample", property_address="1 Tab Way", email="p@example.com")

    def mk(name, **kw):
        t = ViolationType.objects.get_or_create(name=name)[0]
        return Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c", **kw)
    v = mk("Weeds", status=S.NOTICE_SENT, cure_days=15, cure_deadline=timezone.localdate() + timedelta(days=9))
    Response.objects.create(violation=v, message="Hello?", from_resident=True)                 # an owner message waiting for a reply
    mk("Shed", status=S.LEDGER_PENDING, fine_amount=100)
    return client.get("/workspace/").content.decode()


def attention_block(html):
    start = html.index('<div id="attn">')
    return html[start:html.index("<script>", start)]


def test_every_status_card_is_a_tab_with_a_label(page):
    block = attention_block(page)
    keys = re.findall(r'data-tab="([^"]+)"', block)
    assert len(keys) >= 4 and len(keys) == len(set(keys))                       # one tab per card, no repeats
    assert len(keys) == block.count('<div class="card"')                         # no card is left outside a tab
    assert all(re.search(rf'data-tab="{k}" data-label="[^"]+"|data-label="[^"]+" data-tab="{k}"', block) for k in keys)
    assert {"todo", "cure", "messages", "approve"} <= set(keys)


def test_the_tab_bar_is_hidden_until_its_script_builds_it_so_the_page_still_works_without_one(page):
    assert re.search(r'<div id="attn-tabs"[^>]*\bhidden\b', page)
    assert "attn-tabs" in page.split('<div id="attn">', 1)[1]                     # the script that fills it in


def test_the_overdue_link_to_messages_still_lands_on_a_card_inside_a_tab(page):
    assert re.search(r'id="awaiting" data-tab="messages"', page)
