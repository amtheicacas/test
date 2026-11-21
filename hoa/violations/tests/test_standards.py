import io
import shutil
import subprocess
from datetime import date, datetime
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from PIL import Image

from violations import standards_search, views
from violations.models import Notice, Owner, Photo, Profile, Violation, ViolationType

pytestmark = pytest.mark.django_db
U = get_user_model()


@pytest.fixture(autouse=True)
def loaded(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    call_command("load_standards", verbosity=0)


def png(color="green"):
    b = io.BytesIO()
    Image.new("RGB", (60, 40), color).save(b, "PNG")
    return SimpleUploadedFile("a.png", b.getvalue(), content_type="image/png")


def manager(client):
    u = U.objects.create_user("mgr", password="pw-for-tests-123")
    Profile.objects.create(user=u, role="manager")
    client.force_login(u)


# ---------- catalog ----------
def test_catalog_loads_from_the_standards_and_is_idempotent():
    n = ViolationType.objects.exclude(code__isnull=True).count()
    assert n >= 75
    call_command("load_standards", verbosity=0)
    assert ViolationType.objects.exclude(code__isnull=True).count() == n
    t = ViolationType.objects.get(code="3.16")
    assert "Coach Lights must be the same" in t.rule_text  # verbatim from the PDF
    assert t.reference == "Community Standards §3.16 Exterior Lighting"
    assert t.default_cure_days == 7  # the standard says bulbs are replaced within one week
    assert ViolationType.objects.get(code="3.30").default_cure_days == 14  # house numbers: 14-day notice
    assert ViolationType.objects.get(code="4.02-e").default_cure_days == 30  # failing plants: 30 days
    assert ViolationType.objects.get(code="4.02-g").rule_text.startswith("(g) Trees")


def test_every_type_has_rule_text_observation_and_cure():
    for t in ViolationType.objects.exclude(code__isnull=True):
        assert t.rule_text.strip() and t.observed_template.strip() and t.cure_template.strip() and t.category, t.code
        assert t.section in t.reference


# ---------- search ----------
@pytest.mark.parametrize("text,expected", [
    ("trash cans left at the curb all week", {"3.22", "3.22-timing", "3.22-debris"}),
    ("car parked in the street overnight", {"3.60-street"}),
    ("hedges are overgrown and untrimmed", {"4.02-f"}),
    ("boat on a trailer in the driveway", {"3.40", "3.57", "3.64"}),
    ("dead shrub needs replacing", {"4.02-e"}),
    ("christmas lights still up in february", {"3.29"}),
    ("dog off leash", {"3.02"}),
    ("window air conditioner", {"3.68"}),
    ("grass is long and weeds", {"4.02-d", "4.05"}),
])
def test_local_search_finds_the_right_section(text, expected):
    top = [t.code for t in standards_search.local_search(text, limit=3)]
    assert expected & set(top), (text, top)


def test_search_by_section_number_and_empty():
    assert standards_search.local_search("3.16")[0].code == "3.16"
    assert standards_search.local_search("   ") == []
    assert standards_search.local_search("zzzz qqqq") == []


def test_search_api_requires_staff_and_returns_drafts(client):
    assert client.get("/violations/standards/search/?q=garage").status_code == 302
    manager(client)
    d = client.get("/violations/standards/search/?q=garage door open").json()
    assert d["results"][0]["code"] in ("3.21-open", "3.21")
    r = d["results"][0]
    assert r["observed"] and r["cure"] and r["rule"] and r["cure_days"]
    assert d["ai_available"] is False


# ---------- AI assist ----------
def fake_client(text, stop_reason="end_turn"):
    block_thinking = mock.Mock(type="thinking")
    block_text = mock.Mock(type="text", text=text)
    resp = mock.Mock(content=[block_thinking, block_text], stop_reason=stop_reason)
    client = mock.Mock()
    client.messages.create.return_value = resp
    return client


def test_ai_off_without_key(settings):
    settings.ANTHROPIC_API_KEY = ""
    assert standards_search.ai_search("anything") is None


def test_ai_returns_ranked_matches_and_sends_no_personal_data(settings):
    settings.ANTHROPIC_API_KEY = "k"
    Owner.objects.create(name="Zebediah Quillfeather", property_address="99 Unique Heron Way", email="zq@example.com")
    reply = '{"matches":[{"code":"3.22-timing","reason":"Bins out all week."},{"code":"NOPE","reason":"x"}]}'
    c = fake_client(reply)
    with mock.patch("anthropic.Anthropic", return_value=c):
        out = standards_search.ai_search("Bins have been at the curb since Monday")
    assert [(t.code, why) for t, why in out] == [("3.22-timing", "Bins out all week.")]  # unknown codes dropped
    kw = c.messages.create.call_args.kwargs
    assert kw["model"] == settings.AI_MODEL and kw["output_config"]["format"]["type"] == "json_schema"
    sent = kw["messages"][0]["content"]
    assert "Bins have been at the curb" in sent and "3.22-timing |" in sent
    assert "Zebediah" not in sent and "Unique Heron" not in sent and "zq@example.com" not in sent  # no owner data sent


@pytest.mark.parametrize("c", [fake_client("{}", stop_reason="refusal"), fake_client("not json")])
def test_ai_failure_modes_fall_back_to_none(settings, c):
    settings.ANTHROPIC_API_KEY = "k"
    with mock.patch("anthropic.Anthropic", return_value=c):
        assert standards_search.ai_search("x") is None


def test_ai_api_error_falls_back(settings):
    settings.ANTHROPIC_API_KEY = "k"
    c = mock.Mock()
    c.messages.create.side_effect = RuntimeError("down")
    with mock.patch("anthropic.Anthropic", return_value=c):
        assert standards_search.ai_search("x") is None


def test_search_endpoint_puts_ai_matches_first(client, settings):
    settings.ANTHROPIC_API_KEY = "k"
    manager(client)
    with mock.patch("anthropic.Anthropic", return_value=fake_client('{"matches":[{"code":"3.14","reason":"Tools outside."}]}')):
        d = client.get("/violations/standards/search/?q=lawn mower left outside&ai=1").json()
    assert d["ai"] is True and d["results"][0]["code"] == "3.14" and d["results"][0]["reason"] == "Tools outside."
    with mock.patch("anthropic.Anthropic", return_value=fake_client("garbage")):
        d = client.get("/violations/standards/search/?q=lawn mower&ai=1").json()
    assert d["ai"] is False and "isn't available" in d["ai_note"] and d["results"]


# ---------- form: review before sending ----------
def _post(client, owner, vt, **kw):
    data = {"owner": owner.pk, "vtype": vt.pk, "observed_on": date.today().isoformat(), "cure_days": 15,
            "description": "d", "cure_required": "c", "photos": png()}
    data.update(kw)
    return client.post("/violations/new/", data)


def test_drafted_text_with_brackets_cannot_be_sent(client):
    manager(client)
    o = Owner.objects.create(name="Pat", property_address="1 A St", email="p@example.com")
    t = ViolationType.objects.get(code="3.22-timing")
    r = _post(client, o, t, description=t.observed_template, cure_required=t.cure_template, timing="standard")
    assert r.status_code == 200 and b"bracketed" in r.content
    assert Violation.objects.count() == 0
    r = _post(client, o, t, description="Containers were at the curb on Mon 11/2 at 9 a.m.", cure_required=t.cure_template, timing="standard")
    v = Violation.objects.get()
    assert r.status_code == 302 and v.status == "notice_sent" and v.vtype == t


def test_new_form_page_has_picker_and_catalog(client):
    manager(client)
    body = client.get("/violations/new/").content.decode()
    assert "What&rsquo;s the violation?" in body and "catalog-data" in body and "Exterior lighting" in body
    assert "Ask AI" not in body  # hidden until an API key is configured
    assert "<button>Submit Violation</button>" in body and "<button>Save</button>" not in body


def test_ask_ai_button_appears_with_key(client, settings):
    settings.ANTHROPIC_API_KEY = "k"
    manager(client)
    assert "Ask AI" in client.get("/violations/new/").content.decode()


# ---------- letter ----------
def _text(pdf):
    if not shutil.which("pdftotext"):
        pytest.skip("pdftotext not installed")
    return subprocess.run(["pdftotext", "-layout", "-", "-"], input=pdf, capture_output=True).stdout.decode()


def make_nov(settings, **kw):
    settings.HOA_ADDRESS_LINE1, settings.HOA_ADDRESS_ZIP = "PO Box 1234", "34241"
    settings.HOA_PHONE, settings.HOA_CONTACT_EMAIL = "941-555-0100", "board@example.org"
    o = Owner.objects.create(name="Pat Owner", property_address="12 Palm Way", zip_code="34241")
    t = ViolationType.objects.get(code="3.16")
    v = Violation.objects.create(owner=o, vtype=t, description="Coach light bulbs do not match.", cure_required="Replace bulbs.",
                                 cure_days=7, cure_deadline=date(2026, 12, 1), status="notice_sent", **kw)
    Photo.objects.create(violation=v, image=png(), taken_at=datetime(2026, 10, 27, 10, 15).astimezone())
    return Notice.objects.create(violation=v, kind="violation")


def test_letter_follows_the_courtesy_notice_format(settings):
    from violations.pdf import render_notice_pdf
    txt = _text(render_notice_pdf(make_nov(settings)))
    for must in ["Heron Landing Homeowners", "PO Box 1234", "941-555-0100", "VIOLATION:", "Exterior lighting (bulbs, fixtures)",
                 "COVENANTS:", "3.16 Exterior Lighting", "Coach Lights must be the same", "Coach light bulbs do not match.",
                 "On Behalf of the Board of Directors", "Violation Correction Response Form", "I cannot resolve this issue within 7 days",
                 "board@example.org", "Correction Response Form within 7 days"]:
        assert must in txt, must
    assert "Sunstate" not in txt


def test_letter_photo_has_generated_stamp_above_and_taken_caption_below(settings):
    import re
    from violations.pdf import render_notice_pdf
    t = _text(render_notice_pdf(make_nov(settings)))
    assert re.search(r"\b\d{1,2}/\d{1,2}/\d{2} \d{1,2}:\d{2} [AP]M\b", t)  # when the letter was generated
    assert "Tuesday - October 27, 2026 - 10:15 AM" in t  # when the photo was taken


def test_hearing_notice_still_attaches_the_violation_notice(settings):
    from violations.pdf import render_notice_pdf
    n = make_nov(settings)
    v = n.violation
    v.proposed_fine, v.board_meeting_date = 100, date(2026, 11, 10)
    from django.utils import timezone
    v.hearing_date, v.hearing_location = timezone.now(), "Zoom https://z.us/1"
    v.save()
    h = Notice.objects.create(violation=v, kind="hearing")
    txt = _text(render_notice_pdf(h))
    assert "Zoom https://z.us/1" in txt and "ATTACHMENT" in txt and "Coach Lights must be the same" in txt
    assert "Sunstate" not in txt


def test_photo_taken_time_comes_from_exif():
    b = io.BytesIO()
    ex = Image.Exif()
    ex[306] = "2026:10:27 10:15:00"
    Image.new("RGB", (20, 20)).save(b, "JPEG", exif=ex)
    f = SimpleUploadedFile("a.jpg", b.getvalue(), content_type="image/jpeg")
    t = views._taken_at(f)
    assert (t.year, t.month, t.day, t.hour, t.minute) == (2026, 10, 27, 10, 15)
    assert views._taken_at(png()) is None


@pytest.mark.django_db
def test_progress_step_labels():
    from violations.models import Owner, ViolationType
    v = Violation.objects.create(owner=Owner.objects.create(name="L", property_address="1 Label Way"), vtype=ViolationType.objects.create(name="L"),
                                 description="d", cure_required="c")
    labels = [s["title"] for s in v.progress]
    assert labels[2] == "Fine issued by Board"
    assert labels[3] == "Violation sent to Compliance Committee / Homeowner Hearing"
    assert labels[4] == "Fine Imposed & Posted to Ledger"


def test_logo_is_in_app_and_letterhead(client, settings):
    from violations import pdf
    assert pdf.LOGO.exists() and pdf.LOGO.stat().st_size > 1000
    page = client.get("/accounts/login/").content.decode()
    assert "heron-landing-logo.png" in page
    n = make_nov(settings)
    assert pdf.render_notice_pdf(n)[:4] == b"%PDF"
    img = shutil.which("pdfimages")
    if img:  # the logo and the violation photo are both embedded in page 1
        out = subprocess.run([img, "-list", "-f", "1", "-l", "1", "-"], input=pdf.render_notice_pdf(n), capture_output=True).stdout.decode()
        assert len([l for l in out.splitlines()[2:] if l.strip()]) >= 2


def test_weeds_finds_the_beds_and_lawn_clauses(db):
    from django.core.management import call_command
    from violations import standards_search
    call_command("load_standards")
    top = [t.code for t in standards_search.local_search("weeds", limit=4)]
    assert "4.02-a" in top[:2] and "4.05-w" in top[:2], top
    assert [t.code for t in standards_search.local_search("weeds in planter")][0] == "4.02-a"
    assert [t.code for t in standards_search.local_search("weeds in my lawn")][0] == "4.05-w"
