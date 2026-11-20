"""A1 + A2: the exact copy of every letter sent is kept (add-only), and the hearing notice attaches the saved ORIGINAL Notice of Violation."""
import hashlib
import io
from datetime import date, timedelta
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.utils import timezone
from pypdf import PdfReader

from violations import letters, mailing, services
from violations.models import Event, Notice, Owner, Profile, SentLetter, Violation, ViolationType
from violations.pdf import render_notice_pdf

S = Violation.Status
K = Notice.Kind
pytestmark = pytest.mark.django_db
U = get_user_model()


@pytest.fixture(autouse=True)
def _settings(settings):
    settings.HOA_PHONE, settings.HOA_CONTACT_EMAIL = "941-555-0100", "board@example.org"
    settings.HOA_ADDRESS_LINE1, settings.HOA_ADDRESS_ZIP = "PO Box 1234", "34241"


@pytest.fixture
def lob(settings):
    settings.MAIL_PROVIDER, settings.LOB_API_KEY = "lob", "test_key"
    settings.HOA_RETURN_LINE1, settings.HOA_RETURN_ZIP = "PO Box 1", "34241"
    settings.HEARING_REGULAR_COPY = False


def staff(name=None, role="board"):
    name = name or f"boss{U.objects.count()}"
    u = U.objects.create_user(name, f"{name}@example.com", "x", first_name="Bo", last_name="Ard")
    Profile.objects.create(user=u, role=role)
    return u


def make_case(tenant=False, status=S.NOTICE_SENT, address=None):
    o = Owner.objects.create(name="Pat Owner", property_address=address or f"{12 + Owner.objects.count()} Palm Way", city="Sarasota", state="FL", zip_code="34240", email="pat@example.com",
                             tenant_name="Tina Tenant" if tenant else "", tenant_email="tina@example.com" if tenant else "")
    t = ViolationType.objects.get_or_create(name="Shed", defaults={"default_cure_days": 15})[0]
    return Violation.objects.create(owner=o, vtype=t, description="Shed is in disrepair", cure_required="Repair the shed", cure_days=15,
                                    status=status, cure_deadline=date.today() + timedelta(days=15))


def hearing_notice(v, fine=250):
    v.status, v.board_meeting_date, v.proposed_fine = S.HEARING_SCHEDULED, date.today(), fine
    v.hearing_date, v.hearing_location = timezone.now() + timedelta(days=21), "Clubhouse, 1 Heron Way"
    v.save()
    return Notice.objects.create(violation=v, kind=K.HEARING)


def pdf_text(data):
    return " ".join(" ".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages).split())


# ================= A1: the exact copy of every letter sent =================
def test_an_emailed_letter_is_saved_exactly_as_it_was_attached():
    v, u = make_case(), staff()
    n = services.send_notice(v, K.VIOLATION, u)
    sent = mail.outbox[0].attachments[0][1]
    s = n.sent_copies.get()
    assert s.read_bytes() == sent and s.sha256 == hashlib.sha256(sent).hexdigest() and s.size == len(sent)
    assert (s.channel, s.recipient, s.kind, s.sent_by, s.origin) == ("email", "owner", "violation", u, "sent")
    assert s.recipient_name == "Pat Owner" and "pat@example.com" in s.sent_to and s.sent_at <= timezone.now()
    assert s.intact() and s.file.name.startswith("sent-letters/") and s.file.name.endswith(".pdf") and "pat" not in s.file.name.lower()


def test_the_owner_and_the_tenant_each_get_their_own_saved_letter():
    v = make_case(tenant=True)
    n = services.send_notice(v, K.VIOLATION, staff())
    copies = {c.recipient: c for c in n.sent_copies.all()}
    assert set(copies) == {"owner", "tenant"} and copies["tenant"].recipient_name == "Tina Tenant"
    assert "tina@example.com" in copies["tenant"].sent_to and copies["owner"].sha256 != copies["tenant"].sha256
    assert {a[1] for m in mail.outbox for a in m.attachments} == {c.read_bytes() for c in copies.values()}


def test_a_letter_mailed_by_certified_mail_is_saved_with_its_tracking_number(lob):
    v, u = make_case(), staff()
    n = hearing_notice(v)
    sent = []

    def fake_lob(addr, pdf, description, extra=None):
        sent.append((pdf, extra))
        return {"id": "ltr_1", "tracking_number": "9407100000000000000001"}
    with mock.patch.object(mailing, "lob_send", side_effect=fake_lob):
        mailing.dispatch(n, lambda who: render_notice_pdf(n, who), u)
    s = n.sent_copies.get()
    assert (s.channel, s.recipient, s.tracking_number, s.sent_by) == ("certified", "owner_property", "9407100000000000000001", u)
    assert s.read_bytes() == sent[0][0] and s.mail_piece == n.pieces.get() and "Palm Way" in s.sent_to
    assert sent[0][1] == "certified_return_receipt"


def test_a_held_mailing_released_later_is_saved_when_it_is_mailed(lob):
    v = make_case()
    n = services.send_notice(v, K.VIOLATION, staff(), hold_mail_days=2)
    assert [c.channel for c in n.sent_copies.all()] == ["email"] and n.mail_held        # the email copy exists; the mailed copy does not yet
    boss = staff("boss2")
    with mock.patch.object(mailing, "lob_send", return_value={"id": "ltr_2", "tracking_number": ""}):
        services.mail_now(n, boss)
    mailed = n.sent_copies.get(channel="first_class")
    assert mailed.sent_by == boss and mailed.read_bytes()[:4] == b"%PDF"


def test_identical_copies_share_one_stored_file(lob):
    v = make_case()
    n = Notice.objects.create(violation=v, kind=K.VIOLATION)
    a = letters.save_sent_copy(n, b"%PDF-1 same", "owner", "email")
    b = letters.save_sent_copy(n, b"%PDF-1 same", "owner", "first_class")
    assert a.file.name == b.file.name and a.pk != b.pk and a.intact() and b.intact()


def test_saved_copies_can_never_be_changed_or_deleted():
    n = Notice.objects.create(violation=make_case(), kind=K.VIOLATION)
    s = letters.save_sent_copy(n, b"%PDF-1 original", "owner", "email")
    s.note = "edited"
    with pytest.raises(ValidationError):
        s.save()
    with pytest.raises(PermissionDenied):
        s.delete()
    assert SentLetter.objects.get(pk=s.pk).note == ""
    from django.contrib import admin as dj_admin
    ma = dj_admin.site._registry[SentLetter]
    assert not (ma.has_add_permission(None) or ma.has_change_permission(None, s) or ma.has_delete_permission(None, s))


def test_a_storage_failure_never_stops_the_letter_and_is_written_on_the_case():
    v = make_case()
    with mock.patch.object(letters, "save_sent_copy", side_effect=OSError("disk full")):
        n = services.send_notice(v, K.VIOLATION, staff())
    assert mail.outbox and n.emailed_at                                       # the owner still got the letter
    assert Event.objects.filter(violation=v, text__startswith="WARNING: the exact copy").exists()


def test_a_tampered_file_is_caught_and_refused(client):
    v, u = make_case(), staff()
    n = services.send_notice(v, K.VIOLATION, u)
    s = n.sent_copies.get()
    client.force_login(u)
    assert client.get(f"/letters/{s.pk}/").status_code == 200
    with open(s.file.path, "ab") as f:
        f.write(b"tampered")
    assert not s.intact() and client.get(f"/letters/{s.pk}/").status_code == 409


def test_the_case_page_lists_the_saved_letters_and_opens_the_exact_copy(client):
    v, u = make_case(), staff()
    n = services.send_notice(v, K.VIOLATION, u)
    s = n.sent_copies.get()
    client.force_login(u)
    page = client.get(f"/violations/{v.pk}/").content.decode()
    assert "Exact copies of letters sent" in page and f"/letters/{s.pk}/" in page and s.sha256[:10] in page and "Download the case file" in page
    r = client.get(f"/letters/{s.pk}/")
    assert r.status_code == 200 and r["Content-Type"] == "application/pdf" and r.content == s.read_bytes()


def test_only_the_board_and_manager_can_open_a_saved_letter(client):
    v = make_case()
    s = services.send_notice(v, K.VIOLATION, staff()).sent_copies.get()
    assert client.get(f"/letters/{s.pk}/").status_code == 302                    # not signed in
    for role in ("committee", "homeowner"):
        client.force_login(staff(f"x-{role}", role))
        assert client.get(f"/letters/{s.pk}/").status_code == 403
    client.force_login(staff("mgr", "manager"))
    assert client.get(f"/letters/{s.pk}/").status_code == 200


def test_notices_sent_before_copies_were_kept_are_listed_and_flagged(client):
    v = make_case()
    old = Notice.objects.create(violation=v, kind=K.VIOLATION, emailed_at=timezone.now())      # an old notice: no saved copy
    new = services.send_notice(v, K.SECOND, staff())
    assert list(letters.without_saved_copy()) == [old] and new.sent_copies.exists()
    out = io.StringIO()
    call_command("notices_without_copy", stdout=out)
    assert "1 notice(s) have no saved exact copy" in out.getvalue() and "Notice of violation: 1" in out.getvalue() and "Palm Way" in out.getvalue()
    client.force_login(staff())
    assert "No exact copy was saved for: Notice of violation" in client.get(f"/violations/{v.pk}/").content.decode()


def test_the_live_site_needs_a_place_to_keep_files_and_a_bucket_switches_storage_on():
    import os, subprocess, sys
    from violations.tests.test_deploy_safety import HOA_DIR, LIVE
    code = "import config.settings as s; d=s.STORAGES['default']; print(d['BACKEND'], d.get('OPTIONS',{}).get('bucket_name'), d.get('OPTIONS',{}).get('default_acl'), d.get('OPTIONS',{}).get('querystring_auth'))"
    base = {k: v for k, v in os.environ.items() if k not in ("MEDIA_ROOT", "STORAGE_BUCKET")}
    run = lambda **extra: subprocess.run([sys.executable, "-c", code], cwd=HOA_DIR, env={**base, **extra, "HOA_ENV_FILE": "no-such-file"}, capture_output=True, text=True)
    no_files = {k: v for k, v in LIVE.items() if k != "MEDIA_ROOT"}
    assert "File storage must be set" in run(**no_files).stderr
    assert run(**no_files, STORAGE_BUCKET="hoa-private").stdout.split() == ["storages.backends.s3.S3Storage", "hoa-private", "None", "True"]
    assert run(DJANGO_DEBUG="1").stdout.split()[0] == "django.core.files.storage.FileSystemStorage"


def test_photos_are_read_through_the_storage_so_letters_work_on_cloud_storage():
    """Cloud storage has no file path; the letter used to lose its photo silently there."""
    from django.core.files.base import ContentFile
    from PIL import Image
    from violations.models import Photo
    v = make_case()
    b = io.BytesIO()
    Image.new("RGB", (60, 40), "green").save(b, "PNG")
    Photo.objects.create(violation=v, image=ContentFile(b.getvalue(), name="p.png"))
    n = Notice.objects.create(violation=v, kind=K.VIOLATION)
    with_path = len(PdfReader(io.BytesIO(render_notice_pdf(n, "owner"))).pages[0].images)        # the logo and the photo
    with mock.patch("django.db.models.fields.files.FieldFile.path", new_callable=mock.PropertyMock, side_effect=NotImplementedError):
        data = render_notice_pdf(n, "owner")
    assert with_path >= 2 and len(PdfReader(io.BytesIO(data)).pages[0].images) == with_path


# ================= A2: the hearing notice attaches the saved ORIGINAL notice =================
def test_the_hearing_notice_attaches_the_exact_saved_original_notice_of_violation():
    v = make_case()
    orig = services.send_notice(v, K.VIOLATION, staff()).sent_copies.get()
    hn = hearing_notice(v)
    merged = render_notice_pdf(hn, "owner")
    pages = PdfReader(io.BytesIO(merged)).pages
    saved_pages = PdfReader(io.BytesIO(orig.read_bytes())).pages
    text = [(p.extract_text() or "") for p in pages]
    assert "NOTICE OF PROPOSED FINE AND OPPORTUNITY FOR HEARING" in text[0] and "Notice of Violation dated" in " ".join(text[0].split())
    assert "ATTACHMENT - Copy of Notice of Violation" in text[len(pages) - len(saved_pages) - 1]       # the title page, then the saved copy
    assert text[-len(saved_pages):] == [(p.extract_text() or "") for p in saved_pages]                  # the attached pages ARE the saved pages
    assert "Violation Correction Response Form" in text[-1]                                           # including the page the original had


def test_the_attached_copy_is_the_original_and_never_the_second_notice():
    v = make_case()
    orig = services.send_notice(v, K.VIOLATION, staff()).sent_copies.get()
    services.send_notice(v, K.SECOND, staff())
    merged = pdf_text(render_notice_pdf(hearing_notice(v), "owner"))
    assert "Second Notice of Violation" not in merged
    first_date = timezone.localtime(orig.notice.created_at)
    assert f"Notice of Violation dated {first_date:%m/%d/%Y}, copy attached" in merged


def test_the_attachment_is_a_saved_copy_not_a_rebuilt_one(settings):
    """If the case changes after the notice went out, the attached copy still shows what was actually sent."""
    v = make_case()
    orig = services.send_notice(v, K.VIOLATION, staff()).sent_copies.get()
    v.description, v.cure_required = "Something completely different now", "Do something else entirely"
    v.save()
    merged = pdf_text(render_notice_pdf(hearing_notice(v), "owner"))
    assert "Shed is in disrepair" in merged and "Repair the shed" in merged and "Something completely different now" not in merged


def test_without_a_saved_original_a_rebuilt_copy_is_attached_and_the_case_says_so():
    v = make_case()
    Notice.objects.create(violation=v, kind=K.VIOLATION)                      # an old notice with no saved copy
    assert letters.original_notice_copy(v) is None
    pages = PdfReader(io.BytesIO(render_notice_pdf(hearing_notice(v), "owner"))).pages
    assert any("ATTACHMENT - Copy of Notice of Violation" in (p.extract_text() or "") for p in pages)     # today's behavior, kept as the fallback
    v2 = make_case(address="99 Other Way")
    Notice.objects.create(violation=v2, kind=K.VIOLATION)
    v2.status, v2.hearing_date, v2.committee_meeting = S.HEARING_SCHEDULED, timezone.now() + timedelta(days=30), None
    v2.save()
    services.send_notice(v2, K.HEARING, staff())
    assert Event.objects.filter(violation=v2, text__startswith="NOTE: no exact copy of the original Notice of Violation").exists()


def test_the_tenants_hearing_notice_carries_the_tenants_saved_original():
    v = make_case(tenant=True)
    n = services.send_notice(v, K.VIOLATION, staff())
    tenant_copy = n.sent_copies.get(recipient="tenant")
    assert letters.original_notice_copy(v, "tenant") == tenant_copy
    assert letters.original_notice_copy(v, "owner").recipient == "owner"
    assert letters.original_notice_copy(v, "owner_property").recipient == "owner"       # the property-address version falls back to the owner's
    merged = pdf_text(render_notice_pdf(hearing_notice(v), "tenant"))
    assert "Dear Tina Tenant" in merged


def test_the_hearing_goes_by_email_and_certified_mail_each_with_the_saved_original(lob):
    v, u = make_case(), staff()
    orig = services.send_notice(v, K.VIOLATION, u).sent_copies.get(channel="email")
    v.status, v.board_meeting_date, v.proposed_fine = S.HEARING_NEEDED, date.today(), 250
    v.save()
    from violations.models import CommitteeMeeting
    m = CommitteeMeeting.objects.create(starts_at=timezone.now() + timedelta(days=30))
    v.committee_meeting, v.hearing_date, v.hearing_location, v.status = m, m.starts_at, "Clubhouse", S.HEARING_SCHEDULED
    v.save()
    mail.outbox.clear()
    mailed = []

    def fake_lob(addr, pdf, description, extra=None):
        mailed.append((pdf, extra))
        return {"id": "ltr_h", "tracking_number": "9407100000000000000002"}
    with mock.patch.object(mailing, "lob_send", side_effect=fake_lob):
        n = services.send_notice(v, K.HEARING, u)
    emailed = mail.outbox[0].attachments[0][1]
    assert mailed and mailed[0][1] == "certified_return_receipt"                        # certified, return receipt
    saved_text = [(p.extract_text() or "") for p in PdfReader(io.BytesIO(orig.read_bytes())).pages]
    for label, data in (("email", emailed), ("certified mail", mailed[0][0])):
        pages = [(p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages]
        assert pages[-len(saved_text):] == saved_text, label
    kept = {c.channel: c for c in n.sent_copies.all()}
    assert set(kept) == {"email", "certified"} and kept["email"].read_bytes() == emailed and kept["certified"].read_bytes() == mailed[0][0]
    assert kept["certified"].tracking_number == "9407100000000000000002"
