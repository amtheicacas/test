"""Awkward but real-world data (second stabilisation pass): accents, apostrophes, ampersands, emoji, right-to-left names, very long names, line breaks and control
characters pasted from Word, HTML-looking text. A letter, an email, the case-file download or the pages must never fail because of what someone typed or an
import brought in. (The letters' wording and layout are not tested here: pdf.py is frozen until the attorney's final letters.)"""
import io
import zipfile
from unittest import mock

import pytest
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta

from violations import mailing, services
from violations.models import CommitteeMeeting, EmailLog, Notice, Owner, Violation, ViolationType
from violations.tests.review4_support import body

pytestmark = pytest.mark.django_db
AWKWARD = ["O'Brien & Sons, LLC", "José Ñuñez-Peña", "Наталья Иванова", "李小龍", "محمد علي", "😀 Smiley Family", "<b>bold</b> <i>x", "<script>alert(1)</script>",
           "a < b > c & d", "x\x0by", "x\x0cy", "x\x1fy", "x\x7fy", "  spaced  ", "Line1\nLine2", "Tab\there", "A" * 300, "&amp; &lt;", "{{7*7}} %s {0}",
           "‮evil", "'; DROP TABLE x;--", "=HYPERLINK(\"http://evil\",\"x\")", "-1+1", "@SUM(1)"]


def awkward_case(txt, status="hearing_scheduled"):
    o = Owner.objects.create(name=txt[:200], property_address=(txt + " Way")[:250], city=txt[:30], state="FL", zip_code="34240", email="p@example.com",
                             mailing_address=txt, mail_line1=txt[:200], mail_city="Tampa", mail_state="FL", mail_zip="33601", tenant_name=txt[:200],
                             tenant_email="t@example.com")
    t = ViolationType.objects.create(name=txt[:100])
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=30), zoom_link="https://z.example/1", location=txt[:250], zoom_passcode=txt[:40], zoom_dial_in=txt)
    return Violation.objects.create(owner=o, vtype=t, description=txt, cure_required=txt, status=status, proposed_fine=100, fine_amount=100, committee_meeting=m,
                                    hearing_date=m.starts_at, committee_findings=txt, hearing_location=txt[:200], suspension=txt[:200])


@pytest.mark.parametrize("txt", AWKWARD)
def test_every_kind_of_letter_builds_for_every_recipient_whatever_the_text(txt):
    v = awkward_case(txt)
    bad = []
    for kind in Notice.Kind.values:
        n = Notice.objects.create(violation=v, kind=kind)
        for who in ("owner", "owner_property", "tenant"):
            try:
                assert services.render_notice_pdf(n, who).startswith(b"%PDF")
            except Exception as e:      # noqa: BLE001
                bad.append((kind, who, type(e).__name__))
    assert not bad, bad[:4]


@pytest.mark.parametrize("txt", AWKWARD)
def test_sending_a_notice_with_awkward_text_emails_and_mails_it_and_nothing_is_lost(txt, settings):
    settings.MAIL_PROVIDER, settings.LOB_API_KEY, settings.HOA_RETURN_LINE1, settings.HOA_RETURN_ZIP = "lob", "test_key", "PO Box 1", "34241"
    v = awkward_case(txt)
    sent = []
    with mock.patch.object(mailing, "lob_send", side_effect=lambda to, pdf, d, extra=None: sent.append(to["name"]) or {"id": f"l{len(sent)}"}):
        n = services.send_notice(v, Notice.Kind.HEARING)
    n.refresh_from_db()
    assert n.email_error == "" and n.emailed_at, n.email_error                       # (a line break in the address used to make every email fail)
    assert {m.to[0] for m in mail.outbox} == {"p@example.com", "t@example.com"}
    assert n.mailed_on and len(sent) >= 2 and not n.provider_note, n.provider_note
    assert all("\n" not in m.subject and "\r" not in m.subject for m in mail.outbox)
    assert not EmailLog.objects.filter(violation=v, ok=False).exists()


@pytest.mark.parametrize("txt", AWKWARD)
def test_the_case_file_download_builds_whatever_the_text(txt, client, django_user_model):
    from violations.models import Profile
    v = awkward_case(txt)
    services.add_response(v, txt, from_resident=True)
    services.log(v, "note " + txt)
    u = django_user_model.objects.create_user("bd9", password="x")
    Profile.objects.create(user=u, role="board")
    client.force_login(u)
    r = client.post(reverse("case_file", args=[v.pk]))
    assert r.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(body(r))).namelist()
    assert names and all("\x00" not in n and not n.startswith("/") and ".." not in n for n in names)
