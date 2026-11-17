"""The Fining Resolution (April 17, 2023): letters and rules at each stage."""
import io
import shutil
import subprocess
from datetime import date, timedelta
from unittest import mock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image

from violations import mailing, services
from violations.models import Notice, Owner, Photo, Violation, ViolationType
from violations.pdf import num_words, render_notice_pdf

S = Violation.Status
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _s(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.HOA_PHONE, settings.HOA_CONTACT_EMAIL = "941-555-0100", "board@example.org"
    settings.HOA_ADDRESS_LINE1, settings.HOA_ADDRESS_ZIP = "PO Box 1234", "34241"


def png():
    b = io.BytesIO()
    Image.new("RGB", (30, 20)).save(b, "PNG")
    return SimpleUploadedFile("a.png", b.getvalue(), content_type="image/png")


def text(pdf):
    if not shutil.which("pdftotext"):
        pytest.skip("pdftotext not installed")
    return " ".join(subprocess.run(["pdftotext", "-", "-"], input=pdf, capture_output=True).stdout.decode().split())


def case(tenant=False, **kw):
    o = Owner.objects.create(name="Pat Owner", property_address=f"{12 + Owner.objects.count()} Palm Way", zip_code="34241", email="p@example.com",
                             tenant_name="Tina Tenant" if tenant else "", tenant_email="t@example.com" if tenant else "")
    t = ViolationType.objects.get_or_create(name="Shed", defaults={"default_cure_days": 15})[0]
    v = Violation.objects.create(owner=o, vtype=t, description="d", cure_required="c", cure_days=15, status=S.NOTICE_SENT,
                                 cure_deadline=date(2026, 11, 1), **kw)
    Photo.objects.create(violation=v, image=png())
    Notice.objects.create(violation=v, kind="violation")
    return v


def hearing_ready(v, fine=250, suspension="", days=None):
    v.status, v.board_meeting_date, v.proposed_fine = S.HEARING_SCHEDULED, date(2026, 11, 10), fine
    v.proposed_suspension, v.proposed_suspension_days = suspension, days
    v.hearing_date, v.hearing_location = timezone.now() + timedelta(days=21), "Clubhouse, 1 Heron Way (also on Zoom)"
    v.save()
    return Notice.objects.create(violation=v, kind="hearing")


def test_number_words():
    assert [num_words(n) for n in (7, 30, 45, 90, 101)] == ["seven", "thirty", "forty-five", "ninety", "one hundred one"]


# ---------- Exhibit 1: Notice of Violation ----------
def test_notice_of_violation_has_the_resolutions_consequences():
    v = case()
    t = text(render_notice_pdf(v.notices.get()))
    for must in ["FIRST CLASS MAIL", "The Board of Directors has information to believe that you",
                 "imposing a fine against you and/or suspending your right to use certain Common Areas and facilities",
                 "file a lawsuit against you to compel your compliance", "attorneys' fees and costs",
                 "Chapter 720 of the Florida Statutes", "after notice, a hearing, and approval by the Compliance Committee"]:
        assert must in t, must
    assert "jointly and severally liable" not in t  # owner-occupied: no tenant paragraph


def test_notice_of_violation_to_rented_home_adds_the_owner_liability_paragraph():
    v = case(tenant=True)
    for who in ("owner", "tenant"):
        t = text(render_notice_pdf(v.notices.get(), who))
        assert "This Notice is also being provided to the record Owner(s) of the Lot" in t
        assert "jointly and severally liable for the conduct of his or her tenants" in t
        assert "or your tenant(s), guest(s), or invitee(s)" in t
    assert "Dear Tina Tenant" in text(render_notice_pdf(v.notices.get(), "tenant"))


# ---------- Exhibit 2: Notice of Hearing ----------
def test_hearing_notice_follows_exhibit_2():
    v = case()
    n = hearing_ready(v)
    n.tracking_number = "9407 1000 0000"
    n.save()
    t = text(render_notice_pdf(n))
    for must in ["Hearing Panel Review", "SENT BY CERTIFIED MAIL RETURN RECEIPT and by regular U.S. Mail #9407 1000 0000", "has not been corrected and/or has resumed",
                 "the Board voted to levy a fine against you in the amount of $250.00",
                 "Pursuant to Section 720.305(2)(b), Florida Statutes, there will be a hearing before the Compliance Committee on",
                 "Clubhouse, 1 Heron Way (also on Zoom)", "to determine whether to confirm or reject the fine and/or suspension that the Board levied",
                 "review, challenge, and respond to any material considered by the Association", "represented by counsel",
                 "no later than seven (7) days prior to the hearing", "imposed after a hearing has been conducted in your absence",
                 "941-555-0100", "board@example.org", "If the Committee rejects the fine and/or suspension, it will not be imposed",
                 "ATTACHMENT", "Notice of Violation dated"]:
        assert must in t, must
    assert "also suspended" not in t and "jointly and severally" not in t


def test_hearing_notice_certified_line_follows_the_regular_copy_setting(settings):
    t = text(render_notice_pdf(hearing_ready(case())))
    assert "SENT BY CERTIFIED MAIL RETURN RECEIPT and by regular U.S. Mail" in t and "#" not in t.split("Dear")[0].split("Mail")[-1][:5]
    settings.HEARING_REGULAR_COPY = False
    t = text(render_notice_pdf(hearing_ready(case())))
    assert "SENT BY CERTIFIED MAIL RETURN RECEIPT" in t and "regular U.S. Mail" not in t


def test_hearing_notice_dates_zoom_block_and_signer(settings):
    from violations.models import CommitteeMeeting
    settings.HOA_SIGNER_NAME = "Adam Rouse"
    v = case()
    n = hearing_ready(v)
    m = CommitteeMeeting.objects.create(starts_at=timezone.localtime().replace(hour=18, minute=0) + timedelta(days=21),
                                        zoom_link="https://zoom.us/j/95827221705", zoom_meeting_id="958 2722 1705",
                                        zoom_passcode="197632", zoom_dial_in="+13052241968,,95827221705# US\n+13017158592,,95827221705# US (Washington DC)")
    v.committee_meeting, v.hearing_date = m, m.starts_at
    v.save()
    t = text(render_notice_pdf(n))
    assert "has not been corrected and/or has resumed. Accordingly, during its meeting on 11/10/2026," in t  # MM/DD/YYYY like your notice
    assert "at 6:00 p.m. by Zoom (information below)" in t
    for must in ["Join Zoom Meeting", "https://zoom.us/j/95827221705", "Meeting ID: 958 2722 1705", "Passcode: 197632",
                 "One tap mobile", "+13017158592,,95827221705# US (Washington DC)", "Adam Rouse On Behalf of the Board of Directors"]:
        assert must in t, must
    m.location = "Clubhouse, 1 Heron Way"
    m.save()
    v.refresh_from_db()
    assert "Clubhouse, 1 Heron Way and by Zoom (information below)" in text(render_notice_pdf(n))
    m.zoom_link = ""
    m.save()
    v.refresh_from_db()
    t = text(render_notice_pdf(n))
    assert "at Clubhouse, 1 Heron Way" in t and "Join Zoom Meeting" not in t


def test_hearing_notice_with_suspension_and_rented_home():
    v = case(tenant=True)
    n = hearing_ready(v, fine=100, suspension="the pool and clubhouse", days=30)
    t = text(render_notice_pdf(n, "tenant"))
    assert "The Association has also suspended your Lot's right to use the pool and clubhouse for a period of thirty (30) days." in t
    assert "In cases where only a non-Owner (e.g., tenant, occupant, or invitee) is fined" in t
    assert "Owners shall likewise be suspended from common facility use" in t
    assert "Owners have the right to attend the Committee Hearing" in t


def test_suspension_only_hearing_notice_drops_the_word_also():
    t = text(render_notice_pdf(hearing_ready(case(), fine=None, suspension="the pool", days=14)))
    assert "the Board voted to suspend your Lot's right to use certain Common Areas" in t
    assert "The Association has suspended your Lot's right to use the pool for a period of fourteen (14) days" in t


# ---------- Exhibit 3: Notice of Imposition ----------
def test_notice_of_imposition_follows_exhibit_3(settings):
    settings.HOA_PAYMENT_ADDRESS = "c/o the Treasurer, PO Box 99, Sarasota, FL 34241"
    v = case()
    hearing_ready(v, fine=250, suspension="the pool", days=30)
    v.status, v.fine_amount, v.suspension, v.suspension_days = S.FINE_APPROVED, 250, "the pool", 30
    v.suspension_effective_on, v.fine_due_on = date(2026, 12, 8), date(2026, 12, 13)
    v.save()
    n = Notice.objects.create(violation=v, kind="fine")
    t = text(render_notice_pdf(n))
    for must in ["CERTIFIED MAIL #", "Re: Notice of Violation dated", "Hearing held",
                 "the Committee determined that the Board properly levied a fine against you in the amount of $250.00, as authorized by law",
                 "The Committee also confirmed to suspend your right to use the pool for a period of thirty (30) days, effective December 08, 2026",
                 "As such, the fine is now imposed against you.", "the suspension is imposed for a period of thirty (30) days",
                 "applies to all Owners, tenants, occupants, guests, licensees, and invitees",
                 "does not prohibit vehicular or pedestrian ingress and egress, including the right to park",
                 "You must pay the fine no later than five (5) days from the date of this letter",
                 "c/o the Treasurer, PO Box 99", "Checks should be made payable to Heron Landing Homeowners Association, Inc.",
                 "filing an action in a court of competent jurisdiction", "Thank you for your prompt attention"]:
        assert must in t, must


def test_imposition_for_suspension_only_has_no_payment_demand():
    v = case()
    hearing_ready(v, fine=None, suspension="the pool", days=30)
    v.status, v.suspension, v.suspension_days, v.suspension_effective_on = S.FINE_APPROVED, "the pool", 30, date(2026, 12, 8)
    v.save()
    t = text(render_notice_pdf(Notice.objects.create(violation=v, kind="fine")))
    assert "You must pay the fine" not in t and "suspension is imposed" in t


# ---------- Board vote and suspension rules ----------
def test_board_needs_a_majority_and_records_the_vote():
    v = case()
    v.status = S.PENDING_BOARD
    v.save()
    for bad in (dict(votes_for=2, votes_against=2), dict(votes_for=1, votes_against=3), dict()):
        with pytest.raises(services.WorkflowError, match="majority"):
            services.board_decide(v, "proceed", date.today(), fine=100, **bad)
    services.board_decide(v, "proceed", date.today(), fine=100, votes_for=3, votes_against=1)
    v.refresh_from_db()
    assert (v.board_votes_for, v.board_votes_against, v.status) == (3, 1, S.HEARING_NEEDED)
    assert v.events.filter(text__contains="(3-1)").exists()


def test_suspension_needs_facilities_and_days():
    v = case()
    v.status = S.PENDING_BOARD
    v.save()
    with pytest.raises(services.WorkflowError, match="number of days"):
        services.board_decide(v, "proceed", date.today(), suspension="the pool", votes_for=3, votes_against=0)
    with pytest.raises(services.WorkflowError, match="Common Areas"):
        services.board_decide(v, "proceed", date.today(), fine=50, suspension_days=10, votes_for=3, votes_against=0)


def test_dismissing_or_extending_needs_no_vote():
    v = case()
    v.status = S.PENDING_BOARD
    v.save()
    services.board_decide(v, "dismiss", date.today())
    v.refresh_from_db()
    assert v.status == S.DISMISSED


def test_twenty_day_recommendation_is_flagged_but_fourteen_is_allowed():
    short = services.create_meeting(timezone.now() + timedelta(days=15), "https://z.us/1")
    ok = services.create_meeting(timezone.now() + timedelta(days=21), "https://z.us/2")
    assert services.short_notice(short) and not services.short_notice(ok)
    v = case()
    v.status, v.proposed_fine = S.HEARING_NEEDED, 100
    v.save()
    services.assign_to_meeting(v, short)  # legal (>= 14 days) even though under the recommended 20


# ---------- Mailing per the Resolution ----------
def _lob(settings):
    settings.MAIL_PROVIDER, settings.LOB_API_KEY, settings.HOA_RETURN_LINE1, settings.HOA_RETURN_ZIP = "lob", "k", "1 Way", "34240"


def _sent(kind, settings, regular=True):
    _lob(settings)
    settings.HEARING_REGULAR_COPY = regular
    v = case()
    hearing_ready(v)
    got = []
    with mock.patch.object(mailing, "lob_send", side_effect=lambda to, pdf, d, extra=None: got.append(extra) or {"id": "x"}):
        services.send_notice(v, kind)
    return got


def test_hearing_is_certified_with_optional_regular_copy(settings):
    assert _sent("hearing", settings) == ["certified_return_receipt", None]
    assert _sent("hearing", settings, regular=False) == ["certified_return_receipt"]


def test_imposition_is_certified_and_violation_is_first_class(settings):
    v = case()
    v.fine_amount, v.fine_due_on, v.hearing_date = 100, date(2026, 12, 1), timezone.now()
    v.save()
    _lob(settings)
    got = []
    with mock.patch.object(mailing, "lob_send", side_effect=lambda to, pdf, d, extra=None: got.append(extra) or {"id": "x"}):
        services.send_notice(v, "fine")
        services.send_notice(v, "violation")
    assert got == ["certified_return_receipt", None]
